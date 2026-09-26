import pytest
from datetime import timedelta
from decimal import Decimal
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Event, Order, OrderPayment, Organizer

from pretix_payone.models import ReferencedPayoneObject
from pretix_payone.payment import PayoneWero


@pytest.fixture
def payone_env():
    with scopes_disabled():
        organizer = Organizer.objects.create(name="Dummy", slug="dummy")
        event = Event.objects.create(
            organizer=organizer,
            name="Dummy",
            slug="dummy",
            plugins="pretix_payone",
            date_from=now(),
            live=True,
            currency="EUR",
        )
        provider = PayoneWero(event)
        provider.settings.set("mid", "12345")
        provider.settings.set("aid", "54321")
        provider.settings.set("portalid", "1234567")
        provider.settings.set("key", "payone-test-key")
        provider.settings.set("method_wero", True)
        provider.settings.set("_enabled", True)

        order = Order.objects.create(
            code="FOOBAR",
            event=event,
            organizer=organizer,
            email="buyer@example.com",
            locale="en",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("13.37"),
            sales_channel=organizer.sales_channels.get(identifier="web"),
        )
        payment = order.payments.create(
            provider="payone_wero",
            amount=order.total,
            info="{}",
            state=OrderPayment.PAYMENT_STATE_CREATED,
        )
        reference = ReferencedPayoneObject.objects.create(
            txid="345678901",
            order=order,
            payment=payment,
        )

    return {
        "organizer": organizer,
        "event": event,
        "order": order,
        "payment": payment,
        "provider": provider,
        "reference": reference,
    }
