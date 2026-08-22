import hashlib
import json
import pytest
from decimal import Decimal
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.http import HttpRequest
from django_scopes import scopes_disabled
from pretix.base.models import InvoiceAddress, Order, OrderPayment, OrderRefund
from pretix.base.payment import PaymentException
from pretix.multidomain.urlreverse import eventreverse

from pretix_payone.models import ReferencedPayoneObject
from pretix_payone.payment import PayoneWero


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def payone_request(event):
    request = HttpRequest()
    request.event = event
    request.session = SessionStore()
    request.META["HTTP_HOST"] = "example.com"
    request.META["SERVER_NAME"] = "example.com"
    request.META["SERVER_PORT"] = "80"
    return request


def webhook_data(env, **overrides):
    data = {
        "key": hashlib.md5(b"payone-test-key").hexdigest(),
        "txid": env["reference"].txid,
        "aid": "54321",
        "portalid": "1234567",
        "mode": "test" if env["order"].testmode else "live",
        "txaction": "appointed",
        "transaction_status": "completed",
        "notify_version": "7.6",
        "sequencenumber": "0",
        "balance": "13.37",
        "receivable": "13.37",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def post_webhook(client, env, **overrides):
    return client.post("/_payone/status/", webhook_data(env, **overrides))


def return_path(env, action):
    action = "cancel" if action == "back" else action
    return eventreverse(
        env["event"],
        "plugins:pretix_payone:return",
        kwargs={
            "order": env["order"].code,
            "payment": env["payment"].pk,
            "hash": hashlib.sha1(env["order"].secret.lower().encode()).hexdigest(),
            "action": action,
        },
    )


@pytest.mark.django_db
def test_wero_request_contains_required_parameters(payone_env):
    env = payone_env
    request = payone_request(env["event"])

    params = env["provider"]._get_payment_params(request, env["payment"])

    assert params["request"] == "authorization"
    assert params["clearingtype"] == "wlt"
    assert params["wallettype"] == "WRO"
    assert params["currency"] == "EUR"
    assert params["amount"] == 1337
    assert params["lastname"] == "Unknown"
    assert len(params["country"]) == 2
    assert params["successurl"].startswith("http")
    assert params["errorurl"].startswith("http")
    assert params["backurl"].startswith("http")
    assert env["provider"]._default_params(False)["api_version"] == "3.11"


@pytest.mark.django_db
def test_wero_normalizes_too_short_name(payone_env):
    env = payone_env
    with scopes_disabled():
        InvoiceAddress.objects.create(
            order=env["order"],
            name_parts={"family_name": "X"},
            country="DE",
        )

    params = env["provider"]._get_payment_params(
        payone_request(env["event"]), env["payment"]
    )

    assert params["lastname"] == "Unknown"
    assert len(params["firstname"] + params["lastname"]) >= 3


@pytest.mark.django_db
def test_wero_is_only_available_for_eur(payone_env):
    env = payone_env
    request = payone_request(env["event"])
    assert env["provider"].is_allowed(request, Decimal("13.37"))
    assert env["provider"].order_change_allowed(env["order"], request)

    env["event"].currency = "USD"
    env["event"].save(update_fields=["currency"])
    provider = PayoneWero(env["event"])

    assert not provider.is_allowed(request, Decimal("13.37"))
    assert not provider.order_change_allowed(env["order"], request)


@pytest.mark.django_db
def test_wero_order_change_honors_hidden_method_unlock(payone_env):
    env = payone_env
    request = payone_request(env["event"])
    hidden_seed = "wero-test-seed"
    unlock_hash = hashlib.sha256((hidden_seed + env["event"].slug).encode()).hexdigest()
    env["provider"].settings.set("_hidden", True)
    env["provider"].settings.set("_hidden_seed", hidden_seed)

    assert not env["provider"].order_change_allowed(env["order"], request)

    request.session["pretix_unlock_hashes"] = [unlock_hash]

    assert env["provider"].order_change_allowed(env["order"], request)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "expected_state", "raises"),
    [
        (
            {
                "Status": "REDIRECT",
                "TxId": "400000001",
                "RedirectUrl": "https://wero.example/authorize",
            },
            OrderPayment.PAYMENT_STATE_CREATED,
            False,
        ),
        (
            {
                "Status": "PENDING",
                "TxId": "400000002",
            },
            OrderPayment.PAYMENT_STATE_PENDING,
            False,
        ),
        (
            {
                "Status": "ERROR",
                "TxId": "400000003",
                "Error": {
                    "ErrorCode": "123",
                    "CustomerMessage": "Payment declined",
                },
            },
            OrderPayment.PAYMENT_STATE_FAILED,
            True,
        ),
    ],
)
def test_wero_api_responses(payone_env, monkeypatch, payload, expected_state, raises):
    env = payone_env
    request = payone_request(env["event"])
    monkeypatch.setattr(
        "pretix_payone.payment.requests.post",
        lambda *args, **kwargs: FakeResponse(payload),
    )

    with scopes_disabled():
        if raises:
            with pytest.raises(PaymentException):
                env["provider"].execute_payment(request, env["payment"])
        else:
            result = env["provider"].execute_payment(request, env["payment"])
            if payload["Status"] == "REDIRECT":
                assert result == payload["RedirectUrl"]
                assert (
                    request.session["payment_payone_order_secret"]
                    == env["order"].secret
                )

    env["payment"].refresh_from_db()
    assert env["payment"].state == expected_state
    assert env["payment"].info_data["TxId"] == payload["TxId"]
    assert ReferencedPayoneObject.objects.filter(
        txid=payload["TxId"], payment=env["payment"]
    ).exists()


@pytest.mark.django_db
def test_appointed_pending_then_completed_confirms_payment(payone_env, client):
    env = payone_env

    response = post_webhook(
        client,
        env,
        transaction_status="pending",
        reasoncode="903",
        balance="0.00",
        receivable="0.00",
    )

    assert response.status_code == 200
    assert response.content == b"TSOK"
    env["payment"].refresh_from_db()
    env["order"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_PENDING
    assert env["order"].status == Order.STATUS_PENDING
    assert env["payment"].info_data["TransactionStatus"] == {
        "TxAction": "appointed",
        "Status": "pending",
        "ReasonCode": "903",
        "SequenceNumber": "0",
    }

    response = post_webhook(
        client,
        env,
        transaction_status="completed",
        balance="13.37",
    )

    assert response.status_code == 200
    env["payment"].refresh_from_db()
    env["order"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert env["order"].status == Order.STATUS_PAID
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_duplicate_and_late_callbacks_are_idempotent(payone_env, client):
    env = payone_env

    for _ in range(2):
        post_webhook(
            client,
            env,
            transaction_status="pending",
            balance="0.00",
            receivable="0.00",
        )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_PENDING

    for _ in range(2):
        post_webhook(
            client,
            env,
            transaction_status="completed",
            balance="13.37",
        )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert len(mail.outbox) == 1

    post_webhook(
        client,
        env,
        transaction_status="pending",
        balance="0.00",
        receivable="0.00",
    )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_appointed_pending_then_failed_fails_payment(payone_env, client):
    env = payone_env

    post_webhook(
        client,
        env,
        transaction_status="pending",
        balance="0.00",
        receivable="0.00",
    )
    response = post_webhook(
        client,
        env,
        txaction="failed",
        transaction_status="completed",
        reasoncode="902",
        balance="",
    )

    assert response.status_code == 200
    env["payment"].refresh_from_db()
    env["order"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_FAILED
    assert env["order"].status == Order.STATUS_PENDING
    assert env["payment"].info_data["TransactionStatus"]["TxAction"] == "failed"
    assert env["payment"].info_data["TransactionStatus"]["ReasonCode"] == "902"
    assert len(mail.outbox) == 1

    response = post_webhook(
        client,
        env,
        txaction="failed",
        transaction_status="completed",
        reasoncode="902",
        balance="",
    )
    assert response.status_code == 200
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_failed_does_not_reverse_confirmed_payment(payone_env, client):
    env = payone_env
    post_webhook(client, env)

    response = post_webhook(
        client,
        env,
        txaction="failed",
        transaction_status="completed",
        reasoncode="902",
        balance="",
    )

    assert response.status_code == 200
    env["payment"].refresh_from_db()
    env["order"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert env["order"].status == Order.STATUS_PAID
    assert env["payment"].info_data["TransactionStatus"]["TxAction"] == "failed"


@pytest.mark.django_db
def test_legacy_appointed_without_transaction_status_is_final(payone_env, client):
    env = payone_env

    response = post_webhook(
        client,
        env,
        transaction_status=None,
    )

    assert response.status_code == 200
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
@pytest.mark.parametrize("txaction", ["paid", "capture"])
def test_paid_and_capture_require_final_settled_balance(payone_env, client, txaction):
    env = payone_env

    post_webhook(
        client,
        env,
        txaction=txaction,
        transaction_status="pending",
        balance="0.00",
    )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_PENDING

    post_webhook(
        client,
        env,
        txaction=txaction,
        transaction_status="completed",
        balance="13.37",
    )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_PENDING

    post_webhook(
        client,
        env,
        txaction=txaction,
        transaction_status="completed",
        balance="0.00",
    )
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "status_code"),
    [
        ("key", "invalid", 403),
        ("aid", "99999", 403),
        ("portalid", "9999999", 403),
        ("mode", "test", 403),
        ("txid", "999999999", 409),
    ],
)
def test_webhook_rejects_invalid_identity(
    payone_env, client, field, value, status_code
):
    response = post_webhook(client, payone_env, **{field: value})
    assert response.status_code == status_code


@pytest.mark.django_db
def test_webhook_rejects_live_callback_for_test_order(payone_env, client):
    env = payone_env
    env["order"].testmode = True
    env["order"].save(update_fields=["testmode"])

    response = post_webhook(client, env, mode="live")

    assert response.status_code == 403


@pytest.mark.django_db
def test_webhook_rejects_unknown_transaction_status(payone_env, client):
    response = post_webhook(
        client,
        payone_env,
        transaction_status="unknown",
    )

    assert response.status_code == 400
    payone_env["payment"].refresh_from_db()
    assert payone_env["payment"].state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_pending_refund_and_cancelation_do_not_create_refund(payone_env, client):
    env = payone_env
    env["payment"].state = OrderPayment.PAYMENT_STATE_CONFIRMED
    env["payment"].save(update_fields=["state"])

    for txaction in ("refund", "cancelation"):
        response = post_webhook(
            client,
            env,
            txaction=txaction,
            transaction_status="pending",
            receivable="0.00",
        )
        assert response.status_code == 200

    with scopes_disabled():
        assert env["payment"].refunds.count() == 0


@pytest.mark.django_db
def test_completed_external_partial_refund_is_created(payone_env, client):
    env = payone_env
    env["payment"].state = OrderPayment.PAYMENT_STATE_CONFIRMED
    env["payment"].save(update_fields=["state"])

    response = post_webhook(
        client,
        env,
        txaction="refund",
        transaction_status="completed",
        receivable="8.37",
        balance="-5.00",
    )

    assert response.status_code == 200
    with scopes_disabled():
        refund = env["payment"].refunds.get()
    assert refund.amount == Decimal("5.00")
    assert refund.state == OrderRefund.REFUND_STATE_EXTERNAL


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("action", "expected_state"),
    [
        ("error", OrderPayment.PAYMENT_STATE_FAILED),
        ("back", OrderPayment.PAYMENT_STATE_CANCELED),
    ],
)
@pytest.mark.parametrize(
    "initial_state",
    [
        OrderPayment.PAYMENT_STATE_CREATED,
        OrderPayment.PAYMENT_STATE_PENDING,
    ],
)
def test_error_and_cancel_returns_finish_open_payment(
    payone_env, client, action, expected_state, initial_state
):
    env = payone_env
    env["payment"].state = initial_state
    env["payment"].save(update_fields=["state"])
    session = client.session
    session["payment_payone_order_secret"] = env["order"].secret
    session.save()

    response = client.get(return_path(env, action))

    assert response.status_code == 302
    env["payment"].refresh_from_db()
    assert env["payment"].state == expected_state


@pytest.mark.django_db
@pytest.mark.parametrize(
    "initial_state",
    [
        OrderPayment.PAYMENT_STATE_CREATED,
        OrderPayment.PAYMENT_STATE_PENDING,
    ],
)
def test_success_return_never_confirms_payment(payone_env, client, initial_state):
    env = payone_env
    env["payment"].state = initial_state
    env["payment"].save(update_fields=["state"])
    session = client.session
    session["payment_payone_order_secret"] = env["order"].secret
    session.save()

    response = client.get(return_path(env, "success"))

    assert response.status_code == 302
    env["payment"].refresh_from_db()
    assert env["payment"].state == OrderPayment.PAYMENT_STATE_PENDING


@pytest.mark.django_db
def test_wero_partial_refund_uses_next_sequence_number(payone_env, monkeypatch):
    env = payone_env
    env["payment"].state = OrderPayment.PAYMENT_STATE_CONFIRMED
    env["payment"].info_data = {
        "TxId": env["reference"].txid,
        "sequencenumber": "4",
    }
    env["payment"].save(update_fields=["state", "info"])
    with scopes_disabled():
        refund = OrderRefund.objects.create(
            order=env["order"],
            payment=env["payment"],
            provider="payone_wero",
            source=OrderRefund.REFUND_SOURCE_ADMIN,
            state=OrderRefund.REFUND_STATE_CREATED,
            amount=Decimal("5.00"),
        )

    captured = {}

    def fake_post(url, data, headers):
        captured.update(data)
        return FakeResponse(
            {
                "Status": "APPROVED",
                "TxId": env["reference"].txid,
            }
        )

    monkeypatch.setattr("pretix_payone.payment.requests.post", fake_post)

    with scopes_disabled():
        env["provider"].execute_refund(refund)

    refund.refresh_from_db()
    env["payment"].refresh_from_db()
    assert refund.state == OrderRefund.REFUND_STATE_DONE
    assert captured["request"] == "refund"
    assert captured["txid"] == env["reference"].txid
    assert captured["sequencenumber"] == 5
    assert captured["amount"] == -500
    assert captured["currency"] == "EUR"
    assert env["payment"].info_data["sequencenumber"] == 5


@pytest.mark.django_db
def test_control_information_contains_latest_transaction_status(payone_env, client):
    env = payone_env
    post_webhook(
        client,
        env,
        transaction_status="pending",
        reasoncode="903",
    )
    env["payment"].refresh_from_db()

    html = env["provider"].payment_control_render(
        payone_request(env["event"]), env["payment"]
    )

    assert "appointed" in html
    assert "pending" in html
    assert "903" in html
