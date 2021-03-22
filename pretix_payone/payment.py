import hashlib
import json
import logging
import requests
import textwrap
import urllib.parse
from collections import OrderedDict
from datetime import timedelta
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _, get_language
from i18nfield.strings import LazyI18nString
from pretix.base.decimal import round_decimal
from pretix.base.forms import SecretKeySettingsField
from pretix.base.forms.questions import guess_country
from pretix.base.models import Event, InvoiceAddress, Order, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix_mollie.utils import refresh_mollie_token
from requests import HTTPError

logger = logging.getLogger(__name__)


class PayoneSettingsHolder(BasePaymentProvider):
    identifier = "payone"
    verbose_name = "PAYONE"
    is_enabled = False
    is_meta = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "payone", event)

    @property
    def test_mode_message(self):
        if self.event.testmode:
            return _(
                "The PAYONE plugin is operating in test mode. No money will actually be transferred."
            )
        return None

    @property
    def settings_form_fields(self):
        fields = [
            (
                "mid",
                forms.CharField(
                    label=_("Merchant ID"),
                    required=True,
                ),
            ),
            (
                "aid",
                forms.CharField(
                    label=_("Sub-Account ID"),
                    required=True,
                ),
            ),
            (
                "portalid",
                forms.CharField(
                    label=_("Portal ID"),
                    required=True,
                ),
            ),
            (
                "key",
                SecretKeySettingsField(
                    label=_("Key"),
                    required=True,
                ),
            ),
        ]
        methods = [
            ("creditcard", _("Credit card")),
            ("giropay", _("giropay")),
            ("sepadebit", _("SEPA direct debit")),
            ("paypal", _("PayPal")),
            # more: https://docs.payone.com/display/public/PLATFORM/General+information
        ]
        d = OrderedDict(
            fields
            + [
                (f"method_{k}", forms.BooleanField(label=v, required=False))
                for k, v in methods
            ]
            + list(super().settings_form_fields.items())
        )
        d.move_to_end("_enabled", last=False)
        return d


class PayoneMethod(BasePaymentProvider):
    method = ""
    abort_pending_allowed = False
    refunds_allowed = True
    invoice_address_mandatory = False
    clearingtype = None  # https://docs.payone.com/display/public/PLATFORM/clearingtype+-+definition
    onlinebanktransfertype = None  # https://docs.payone.com/display/public/PLATFORM/onlinebanktransfertype+-+definition
    onlinebanktransfer_countries = ()
    wallettype = (
        None  # https://docs.payone.com/display/PLATFORM/wallettype+-+definition
    )

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "payone", event)

    @property
    def settings_form_fields(self):
        return {}

    @property
    def identifier(self):
        return "payone_{}".format(self.method)

    @property
    def is_enabled(self) -> bool:
        return self.settings.get("_enabled", as_type=bool) and self.settings.get(
            "method_{}".format(self.method), as_type=bool
        )

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return self.refunds_allowed

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)

    def payment_is_valid_session(self, request: HttpRequest):
        return True

    def payment_form_render(self, request) -> str:
        template = get_template("pretix_payone/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_payone/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
        }
        return template.render(ctx)

    def payment_can_retry(self, payment):
        return self._is_still_available(order=payment.order)

    def payment_pending_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_payone/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
            "order": payment.order,
            "payment": payment,
            "payment_info": payment_info,
        }
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = json.loads(payment.info)
        else:
            payment_info = None
        template = get_template("pretix_payone/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "method": self.method,
            "provider": self,
        }
        return template.render(ctx)

    @property
    def _default_params(self):
        return {
            "aid": self.settings.aid,
            "mid": self.settings.mid,
            "portalid": self.settings.portalid,
            "key": hashlib.md5(self.settings.key.encode()).hexdigest(),
            "api_version": "3.11",
            "mode": "test" if self.event.testmode else "live",
            "encoding": "UTF-8",
        }

    def execute_refund(self, refund: OrderRefund, retry=True):
        raise NotImplementedError

    def _amount_to_decimal(self, cents):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return round_decimal(float(cents) / (10 ** places), self.event.currency)

    def _decimal_to_int(self, amount):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return int(amount * 10 ** places)

    def _get_payment_params(self, request, payment):
        d = {
            "request": "authorization",
            "reference": "{ev}-{code}".format(
                ev=self.event.slug[: 20 - 1 - len(payment.order.code)],
                code=payment.order.code,
            ),
            "amount": self._decimal_to_int(payment.amount),
            "currency": self.event.currency,
            "param": f"{self.event.slug}-{payment.full_id}",
            "narrative_text": "{code} {event}".format(
                code=payment.order.code,
                event=str(self.event.name)[: 81 - 1 - len(payment.order.code)],
            ),
            "customer_is_present": "yes",
            "recurrence": "none",
            "clearingtype": self.clearingtype,
        }

        if self.clearingtype == "sb":
            d["onlinebanktransfertype"] = self.onlinebanktransfertype
            d["bankcountry"] = (
                self.onlinebanktransfer_countries[0]
                if len(self.onlinebanktransfer_countries) == 1
                else "USERSELECTED"
            )  # todo

        if self.clearingtype == "wlt":
            d["wallettype"] = self.wallettype

        if self.clearingtype in ("sb", "wlt", "cc"):
            d["successurl"] = build_absolute_uri(
                self.event,
                "plugins:pretix_payone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hashlib.sha1(
                        payment.order.secret.lower().encode()
                    ).hexdigest(),
                    "action": "success",
                },
            )
            d["errorurl"] = build_absolute_uri(
                self.event,
                "plugins:pretix_payone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hashlib.sha1(
                        payment.order.secret.lower().encode()
                    ).hexdigest(),
                    "action": "error",
                },
            )
            d["backurl"] = build_absolute_uri(
                self.event,
                "plugins:pretix_payone:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment.pk,
                    "hash": hashlib.sha1(
                        payment.order.secret.lower().encode()
                    ).hexdigest(),
                    "action": "cancel",
                },
            )

        try:
            ia = payment.order.invoice_address
        except InvoiceAddress.DoesNotExist:
            ia = InvoiceAddress()

        if ia.company:
            d["company"] = ia.company[:50]

        if ia.name_parts.get("family_name"):
            d["lastname"] = ia["family_name"][:50]
            d["firstname"] = ia.get("given_name", "")[:50]
        elif ia.name:
            d["lastname"] = ia.name.rsplit(" ", 1)[-1][:50]
            d["firstname"] = ia.name.rsplit(" ", 1)[0][:50]
        elif not ia.company:
            d["lastname"] = "Unknown"

        if ia.country:
            d["country"] = str(ia.country)
        else:
            d["country"] = guess_country(self.event) or "DE"

        if ia.vat_id and ia.vat_id_validated:
            d["vatid"] = ia.vatid

        if self.invoice_address_mandatory:
            if ia.name_parts.get("salutation"):
                d["salutation"] = ia.get("salutation", "")[:10]
            if ia.name_parts.get("title"):
                d["title"] = ia.get("title", "")[:20]
            if ia.address:
                d["street"] = ia.address[:50]
            if ia.zipcode:
                d["zip"] = ia.zipcode[:10]
            if ia.city:
                d["city"] = ia.city[:50]
            if ia.state and ia.country in (
                "US",
                "CA",
                "CN",
                "JP",
                "MX",
                "BR",
                "AR",
                "ID",
                "TH",
                "IN",
            ):
                d["state"] = ia.state

        d["language"] = payment.order.locale[:2]
        return d

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        data = dict(**self._get_payment_params(request, payment), **self._default_params)
        try:
            req = requests.post(
                "https://api.pay1.de/post-gateway/",
                data=data,
                headers={"Accept": "application/json"},
            )
            req.raise_for_status()
        except HTTPError:
            logger.exception("PAYONE error: %s" % req.text)
            try:
                d = req.json()
            except:
                d = {"error": True, "detail": req.text}
            payment.fail(info=d)
            raise PaymentException(
                _(
                    "We had trouble communicating with our payment provider. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )

        data = req.json()

        payment.info = json.dumps(data)
        payment.state = OrderPayment.PAYMENT_STATE_CREATED
        payment.save()

        if data["Status"] == "APPROVED":
            payment.confirm()
        elif data["Status"] == "REDIRECT":
            request.session["payment_payone_order_secret"] = payment.order.secret
            return self.redirect(request, data["RedirectUrl"])
        elif data["Status"] == "ERROR":
            payment.fail()
            raise PaymentException(
                _("Our payment provider returned an error message: {message}").format(
                    message=data["Error"].get(
                        "CustomerMessage", data.get("ErrorMessage", "Unknown error")
                    )
                )
            )
        elif data["Status"] == "PENDING":
            payment.state = OrderPayment.PAYMENT_STATE_PENDING
            payment.save()

    def redirect(self, request, url):
        if request.session.get("iframe_session", False):
            signer = signing.Signer(salt="safe-redirect")
            return (
                build_absolute_uri(request.event, "plugins:pretix_payone:redirect")
                + "?url="
                + urllib.parse.quote(signer.sign(url))
            )
        else:
            return str(url)


class PayoneCC(PayoneMethod):
    method = "creditcard"
    verbose_name = _("Credit card via PAYONE")
    public_name = _("Credit card")
    clearingtype = "cc"

    def _get_payment_params(self, request, payment):
        d = super()._get_payment_params(request, payment)
        d["pseudocardpan"] = request.session['payment_payone_pseudocardpan']
        d["cardholder"] = request.session.get('payment_payone_cardholder', '')
        return d

    def payment_is_valid_session(self, request):
        return request.session.get('payment_payone_pseudocardpan', '') != ''

    def payment_prepare(self, request, payment):
        ppan = request.POST.get('payone_pseudocardpan', '')
        if ppan:
            request.session['payment_payone_pseudocardpan'] = ppan
            for f in ('truncatedcardpan', 'cardtypeResponse', 'cardexpiredateResponse', 'cardholder'):
                request.session[f'payment_payone_{f}'] = request.POST.get(f'payone_{f}', '')
        elif not request.session['payment_payone_pseudocardpan']:
            messages.warning(request, _('You may need to enable JavaScript for Stripe payments.'))
            return False
        return True

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            super().execute_payment(request, payment)
        finally:
            request.session.pop('payment_payone_pseudocardpan', None)
            request.session.pop('payment_payone_truncatedcardpan', None)
            request.session.pop('payment_payone_cardtypeResponse', None)
            request.session.pop('payment_payone_cardexpiredateResponse', None)
            request.session.pop('payment_payone_cardholder', None)

    def payment_form_render(self, request) -> str:

        d = {
            "request": "creditcardcheck",
            "responsetype": "JSON",
            "aid": self.settings.aid,
            "mid": self.settings.mid,
            "portalid": self.settings.portalid,
            "mode": "test" if self.event.testmode else "live",
            "encoding": "UTF-8",
            "storecarddata": "yes",
        }

        h = hashlib.md5()
        for k in sorted(d.keys()):
            h.update(d[k].encode())
        h.update(self.settings.key.encode())
        d['hash'] = h.hexdigest()

        lng = get_language()[:2]
        if lng not in ('de', 'en', 'es', 'fr', 'it', 'nl', 'pt'):
            lng = 'en'

        template = get_template("pretix_payone/checkout_payment_form_cc.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings, "req": json.dumps(d), "language": lng}
        return template.render(ctx)


class PayoneGiropay(PayoneMethod):
    method = "giropay"
    verbose_name = _("giropay via PAYONE")
    public_name = _("giropay")
    clearingtype = "sb"
    onlinebanktransfertype = "GPY"
    onlinebanktransfer_countries = ("DE",)
    # todo: ask for country for others like this


class PayoneSEPADebit(PayoneMethod):
    method = "sepadebit"
    verbose_name = _("SEPA direct debit via PAYONE")
    public_name = _("SEPA direct debit")
    clearingtype = "elv"
    invoice_address_mandatory = True
    # todo: ask for account details


class PayonePayPal(PayoneMethod):
    method = "paypal"
    verbose_name = _("PayPal via PAYONE")
    public_name = _("PayPal")
    clearingtype = "wlt"
    wallettype = "PPE"


# todo: sofort, eps, postfinance e-fiance, postfinance card, ideal, przelewy24, bancontact, masterpass, amazon, alipay, paydirekt