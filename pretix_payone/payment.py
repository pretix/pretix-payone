import hashlib
import json
import logging
import requests
import urllib.parse
from collections import OrderedDict
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.safestring import mark_safe
from django.utils.translation import get_language, gettext_lazy as _
from json import JSONDecodeError
from localflavor.generic.forms import IBANFormField
from pretix.base.decimal import round_decimal
from pretix.base.forms import SecretKeySettingsField
from pretix.base.forms.questions import guess_country
from pretix.base.models import Event, InvoiceAddress, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.countries import CachedCountries
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri
from requests import HTTPError, RequestException

from pretix_payone.models import ReferencedPayoneObject

logger = logging.getLogger(__name__)

cardtypes = [
    ("V", _("VISA")),
    ("M", _("MasterCard")),
    ("J", _("JCB")),
    ("A", _("American Express")),
    ("D", _("Diners Club/Discover")),
    ("O", _("Maestro")),
    ("U", _("UTAP/AirPlus")),
    ("P", _("China Union Pay"))
    # https://docs.payone.com/display/public/PLATFORM/cardtype+-+definition
]


class PayoneSettingsHolder(BasePaymentProvider):
    identifier = "payone"
    verbose_name = "PAYONE"
    is_enabled = False
    is_meta = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "payone", event)

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
            ("paypal", _("PayPal")),
            ("eps", _("eps")),  # ???
            ("sofort", _("SOFORT")),
            ("ideal", _("iDEAL")),
            ("giropay", _("giropay")),
            # disabled because they are untested
            # ("przelewy24", _("Przelewy24")),
            # ("multibanco", _("Multibanco")),
            # ("bancontact", _("Bancontact")),
            # ("vkp", _("Verkkopankki")),
            # ("mybank", _("MyBank")),
            # ("alipay", _("Alipay")),
            # ("paydirekt", _("paydirekt")),
            # ("paysafecard", _("paysafecard")),
            # ("qiwi", _("Qiwi")),
            # more: https://docs.payone.com/display/public/PLATFORM/General+information
        ]
        d = OrderedDict(
            fields
            + [
                (f"method_{k}", forms.BooleanField(label=v, required=False))
                for k, v in [methods.pop(0)]
            ]
            + [
                (
                    f"cardtypes_{k}",
                    forms.BooleanField(
                        label="{} {}".format(
                            '<span class="fa fa-credit-card"></span>', v
                        ),
                        required=False,
                        widget=forms.CheckboxInput(
                            attrs={
                                "data-display-dependency": "#id_payment_payone_method_creditcard"
                            }
                        ),
                    ),
                )
                for k, v in cardtypes
            ]
            + [
                (f"method_{k}", forms.BooleanField(label=v, required=False))
                for k, v in methods
            ]
            + list(super().settings_form_fields.items())
        )
        d.move_to_end("_enabled", last=False)
        return d

    def settings_content_render(self, request):
        return "<div class='alert alert-info'>%s<br /><code>%s</code></div>" % (
            _(
                "Please configure the TransactionStatus URL to "
                "the following endpoint in order to automatically cancel orders when charges are refunded externally "
                "and to process asynchronous payment methods like SOFORT."
            ),
            build_global_uri("plugins:pretix_payone:webhook"),
        )


class PayoneMethod(BasePaymentProvider):
    method = ""
    abort_pending_allowed = False
    refunds_allowed = True
    invoice_address_mandatory = False
    clearingtype = None  # https://docs.payone.com/display/public/PLATFORM/clearingtype+-+definition
    onlinebanktransfertype = None  # https://docs.payone.com/display/public/PLATFORM/onlinebanktransfertype+-+definition
    onlinebanktransfer_countries = ()
    consider_appointed_as_paid = True
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
    def test_mode_message(self):
        if self.event.testmode:
            return mark_safe(
                _(
                    "The PayOne plugin is operating in test mode. You can use one of <a {args}>many test "
                    "cards</a> to perform a transaction. No money will actually be transferred."
                ).format(
                    args='href="https://docs.payone.com/display/public/PLATFORM/Testdata" '
                    'target="_blank"'
                )
            )
        return None

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
        if self.payment_form_fields:
            form = self.payment_form(request)
        else:
            form = None
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "form": form,
        }
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
        from pretix import __version__

        from pretix_payone import __version__ as pluginver

        return {
            "aid": self.settings.aid,
            "mid": self.settings.mid,
            "portalid": self.settings.portalid,
            "key": hashlib.md5(self.settings.key.encode()).hexdigest(),
            "api_version": "3.11",
            "mode": "test" if self.event.testmode else "live",
            "encoding": "UTF-8",
            "integrator_name": "rami.io GmbH",
            "integrator_version": pluginver,
            "solution_name": "pretix",
            "solution_version": __version__,
        }

    def matching_id(self, payment: OrderPayment):
        return payment.info_data.get("TxId", None)

    def execute_refund(self, refund: OrderRefund):
        refund_params = {
            "request": "refund",
            "txid": refund.payment.info_data.get("TxId"),
            "sequencenumber": int(refund.payment.info_data.get("sequencenumber", "0"))
            + 1,
            "amount": self._decimal_to_int(refund.amount) * -1,
            "currency": self.event.currency,
            "narrative_text": "{code} {event}".format(
                code=refund.order.code,
                event=str(self.event.name)[: 81 - 1 - len(refund.order.code)],
            ),
            "transaction_param": f"{self.event.slug}-{refund.full_id}",
        }
        data = dict(**refund_params, **self._default_params)
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
            except JSONDecodeError:
                d = {"error": True, "detail": req.text}
            refund.info_data = d
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save()
            raise PaymentException(
                _(
                    "We had trouble communicating with our payment provider. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )
        except RequestException as e:
            logger.exception("PAYONE error: %s" % str(e))
            d = {"error": True, "detail": str(e)}
            refund.info_data = d
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save()
            raise PaymentException(
                _(
                    "We had trouble communicating with our payment provider. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )

        data = req.json()

        if data["Status"] != "ERROR":
            d = refund.payment.info_data
            d["sequencenumber"] = refund_params["sequencenumber"]
            refund.payment.info = json.dumps(d)
            refund.payment.save()

        refund.info = json.dumps(data)

        if data["Status"] == "APPROVED":
            refund.done()
        elif data["Status"] == "PENDING":
            refund.done()  # not technically correct, but we're not sure we'd ever get an udpate.
        elif data["Status"] == "ERROR":
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save()
            raise PaymentException(data["Error"].get("ErrorMessage", "Unknown error"))

    def _amount_to_decimal(self, cents):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return round_decimal(float(cents) / (10**places), self.event.currency)

    def _decimal_to_int(self, amount):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return int(amount * 10**places)

    def _get_payment_params(self, request, payment):
        d = {
            "request": "authorization",
            "reference": "{ev}-{code}".format(
                ev=self.event.slug[: 20 - 1 - len(payment.full_id)],
                code=payment.full_id,
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
            d["lastname"] = ia.name_parts.get("family_name", "")[:50]
            d["firstname"] = ia.name_parts.get("given_name", "")[:50]
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
            d["vatid"] = ia.vat_id

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
        data = dict(
            **self._get_payment_params(request, payment), **self._default_params
        )
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
            except JSONDecodeError:
                d = {"error": True, "detail": req.text}
            payment.fail(info=d)
            raise PaymentException(
                _(
                    "We had trouble communicating with our payment provider. Please try again and get in touch "
                    "with us if this problem persists."
                )
            )
        except RequestException as e:
            logger.exception("PAYONE error: %s" % str(e))
            d = {"error": True, "detail": str(e)}
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

        if "TxId" in data:
            ReferencedPayoneObject.objects.get_or_create(
                txid=data["TxId"],
                payment=payment,
                order=payment.order,
            )

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
            return (
                build_absolute_uri(request.event, "plugins:pretix_payone:redirect")
                + "?data="
                + signing.dumps(
                    {
                        "url": url,
                        "session": {
                            "payment_payone_order_secret": request.session[
                                "payment_payone_order_secret"
                            ],
                        },
                    },
                    salt="safe-redirect",
                )
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
        d["pseudocardpan"] = request.session["payment_payone_pseudocardpan"]
        d["cardholder"] = request.session.get("payment_payone_cardholder", "")
        return d

    def payment_is_valid_session(self, request):
        return request.session.get("payment_payone_pseudocardpan", "") != ""

    def checkout_prepare(self, request: HttpRequest, cart):
        ppan = request.POST.get("payone_pseudocardpan", "")
        if ppan:
            request.session["payment_payone_pseudocardpan"] = ppan
            for f in (
                "truncatedcardpan",
                "cardtypeResponse",
                "cardexpiredateResponse",
                "cardholder",
            ):
                request.session[f"payment_payone_{f}"] = request.POST.get(
                    f"payone_{f}", ""
                )
        elif not request.session.get("payment_payone_pseudocardpan"):
            messages.warning(
                request, _("You may need to enable JavaScript for payments.")
            )
            return False
        return True

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, payment)

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        try:
            return super().execute_payment(request, payment)
        finally:
            request.session.pop("payment_payone_pseudocardpan", None)
            request.session.pop("payment_payone_truncatedcardpan", None)
            request.session.pop("payment_payone_cardtypeResponse", None)
            request.session.pop("payment_payone_cardexpiredateResponse", None)
            request.session.pop("payment_payone_cardholder", None)

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
        d["hash"] = h.hexdigest()

        lng = get_language()[:2]
        if lng not in ("de", "en", "es", "fr", "it", "nl", "pt"):
            lng = "en"

        template = get_template("pretix_payone/checkout_payment_form_cc.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "req": json.dumps(d),
            "language": lng,
            "cardtypes": json.dumps(
                [
                    k
                    for k, v in cardtypes
                    if self.settings.get(f"cardtypes_{k}", False, as_type=bool)
                ]
            ),
        }
        return template.render(ctx)


class PayoneGiropay(PayoneMethod):  # untested
    method = "giropay"
    verbose_name = _("giropay via PAYONE")
    public_name = _("giropay")
    clearingtype = "sb"
    onlinebanktransfertype = "GPY"
    onlinebanktransfer_countries = ("DE",)

    def _get_payment_params(self, request, payment):
        d = super()._get_payment_params(request, payment)
        d["iban"] = request.session["payment_payone_giropay_iban"]
        return d

    @property
    def payment_form_fields(self):
        return OrderedDict(
            [
                (
                    "iban",
                    IBANFormField(
                        label=_("IBAN"),
                        include_countries=["DE"],
                        help_text=_("Please enter a german IBAN starting with DE"),
                    ),
                ),
            ]
        )

    def checkout_prepare(self, request, cart):
        form = self.payment_form(request)
        if form.is_valid():
            request.session["payment_payone_giropay_iban"] = form.cleaned_data["iban"]
            return super().checkout_prepare(request, cart)
        return False

    def payment_is_valid_session(self, request):
        return (
            super().payment_is_valid_session(request)
            and request.session.get("payment_payone_giropay_iban", "") != ""
        )


class PayoneEPS(PayoneMethod):
    method = "eps"
    verbose_name = _("eps via PAYONE")
    public_name = _("eps")
    clearingtype = "sb"
    onlinebanktransfertype = "EPS"
    onlinebanktransfer_countries = ("AT",)
    banks = (
        ("ARZ_OAB", "Apothekerbank"),
        ("ARZ_BAF", "Ärztebank"),
        ("BA_AUS", "Bank Austria"),
        ("ARZ_BCS", "Bankhaus Carl Spängler & Co.AG"),
        ("EPS_SCHEL", "Bankhaus Schelhammer & Schattera AG"),
        ("BAWAG_PSK", "BAWAG P.S.K.AG"),
        ("BAWAG_ESY", "Easybank AG"),
        ("SPARDAT_EBS", "Erste Bank und Sparkassen"),
        ("ARZ_HAA", "Hypo Alpe-Adria-Bank International AG"),
        ("ARZ_VLH", "Hypo Landesbank Vorarlberg"),
        ("HRAC_OOS", "HYPO Oberösterreich, Salzburg, Steiermark"),
        ("ARZ_HTB", "Hypo Tirol Bank AG"),
        ("EPS_OBAG", "Oberbank AG"),
        ("RAC_RAC", "Raiffeisen Bankengruppe Österreich"),
        ("EPS_SCHOELLER", "Schoellerbank AG"),
        ("ARZ_OVB", "Volksbank Gruppe"),
        ("EPS_VRBB", "VR-Bank Braunau"),
        ("EPS_AAB", "Austrian Anadi Bank AG"),
        ("EPS_BKS", "BKS Bank AG"),
        ("EPS_BKB", "Brüll Kallmus Bank AG"),
        ("EPS_VLB", "BTV VIER LÄNDER BANK"),
        ("EPS_CBGG", "Capital Bank Grawe Gruppe AG"),
        ("EPS_DB", "Dolomitenbank"),
        ("EPS_NOEGB", "HYPO NOE Gruppe Bank AG"),
        ("EPS_NOELB", "HYPO NOE Landesbank AG"),
        ("EPS_HBL", "HYPO-BANK BURGENLAND Aktiengesellschaft"),
        ("EPS_MFB", "Marchfelder Bank"),
        ("EPS_SPDBW", "Sparda Bank Wien"),
        ("EPS_SPDBA", "SPARDA-BANK AUSTRIA"),
        ("EPS_VKB", "Volkskreditbank AG"),
    )

    def _get_payment_params(self, request, payment):
        p = super()._get_payment_params(request, payment)
        p["bankgrouptype"] = request.session["payment_payone_eps_bank"]
        return p

    def checkout_prepare(self, request, cart):
        form = self.payment_form(request)
        if form.is_valid():
            request.session["payment_payone_eps_bank"] = form.cleaned_data["bank"]
            return super().checkout_prepare(request, cart)
        return False

    def payment_is_valid_session(self, request):
        return (
            super().payment_is_valid_session(request)
            and request.session.get("payment_payone_eps_bank", "") != ""
        )

    @property
    def payment_form_fields(self):
        return OrderedDict(
            [
                ("bank", forms.ChoiceField(label=_("Bank"), choices=self.banks)),
            ]
        )


class PayoneIdeal(PayoneMethod):
    method = "ideal"
    verbose_name = _("iDEAL via PAYONE")
    public_name = _("iDEAL")
    clearingtype = "sb"
    onlinebanktransfertype = "IDL"
    onlinebanktransfer_countries = ("NL",)
    banks = (
        ("ABN_AMRO_BANK", "ABN Amro"),
        ("BUNQ_BANK", "Bunq"),
        ("RABOBANK", "Rabobank"),
        ("ASN_BANK", "ASN Bank"),
        ("SNS_BANK", "SNS Bank"),
        ("TRIODOS_BANK", "Triodos Bank"),
        ("SNS_REGIO_BANK", "Regio Bank"),
        ("ING_BANK", "ING Bank"),
        ("KNAB_BANK", "Knab"),
        ("VAN_LANSCHOT_BANKIERS", "van Lanschot"),
        ("HANDELSBANKEN", "Handelsbanken"),
        ("MONEYOU", "Moneyou"),
    )

    def _get_payment_params(self, request, payment):
        p = super()._get_payment_params(request, payment)
        p["bankgrouptype"] = request.session["payment_payone_ideal_bank"]
        p["country"] = "NL"
        return p

    def checkout_prepare(self, request, cart):
        form = self.payment_form(request)
        if form.is_valid():
            request.session["payment_payone_ideal_bank"] = form.cleaned_data["bank"]
            return super().checkout_prepare(request, cart)
        return False

    def payment_is_valid_session(self, request):
        return (
            super().payment_is_valid_session(request)
            and request.session.get("payment_payone_ideal_bank", "") != ""
        )

    @property
    def payment_form_fields(self):
        return OrderedDict(
            [
                ("bank", forms.ChoiceField(label=_("Bank"), choices=self.banks)),
            ]
        )


class PayoneSofort(PayoneMethod):
    method = "sofort"
    verbose_name = _("SOFORT via PAYONE")
    public_name = _("SOFORT")
    clearingtype = "sb"
    onlinebanktransfertype = "PNT"
    onlinebanktransfer_countries = (
        "DE",
        "AT",
        "CH",
        "NL",
        "PL",
        "BE",
    )

    def _get_payment_params(self, request, payment):
        p = super()._get_payment_params(request, payment)
        p["bankcountry"] = request.session["payment_payone_sofort_bankcountry"]
        return p

    def checkout_prepare(self, request, cart):
        form = self.payment_form(request)
        if form.is_valid():
            request.session["payment_payone_sofort_bankcountry"] = form.cleaned_data[
                "bankcountry"
            ]
            return super().checkout_prepare(request, cart)
        return False

    def payment_is_valid_session(self, request):
        return (
            super().payment_is_valid_session(request)
            and request.session.get("payment_payone_sofort_bankcountry", "") != ""
        )

    @property
    def payment_form_fields(self):
        countries = CachedCountries()
        return OrderedDict(
            [
                (
                    "bankcountry",
                    forms.ChoiceField(
                        label=_("Bank country"),
                        choices=(
                            (c, countries.name(c))
                            for c in self.onlinebanktransfer_countries
                        ),
                    ),
                ),
            ]
        )


class PayonePrzelewy24(PayoneMethod):
    method = "przelewy24"
    verbose_name = _("Przelewy24 via PAYONE")
    public_name = _("Przelewy24")
    clearingtype = "sb"
    onlinebanktransfertype = "P24"
    onlinebanktransfer_countries = ("PL",)


class PayoneMultibanco(PayoneMethod):
    method = "multibanco"
    verbose_name = _("Multibanco via PAYONE")
    public_name = _("Multibanco")
    clearingtype = "sb"
    onlinebanktransfertype = "MBC"
    onlinebanktransfer_countries = ("PT",)


class PayoneBancontact(PayoneMethod):
    method = "bancontact"
    verbose_name = _("Bancontact via PAYONE")
    public_name = _("Bancontact")
    clearingtype = "sb"
    onlinebanktransfertype = "BCT"
    onlinebanktransfer_countries = ("BE",)


class PayoneVerkkopankki(PayoneMethod):
    method = "vkp"
    verbose_name = _("Verkkopankki via PAYONE")
    public_name = _("Verkkopankki")
    clearingtype = "sb"
    onlinebanktransfertype = "VKP"
    onlinebanktransfer_countries = ("FI",)


class PayoneMyBank(PayoneMethod):
    method = "mybank"
    verbose_name = _("MyBank via PAYONE")
    public_name = _("MyBank")
    clearingtype = "sb"
    onlinebanktransfertype = "MYB"
    onlinebanktransfer_countries = ("IT",)


class PayonePayPal(PayoneMethod):
    method = "paypal"
    verbose_name = _("PayPal via PAYONE")
    public_name = _("PayPal")
    clearingtype = "wlt"
    wallettype = "PPE"


class PayoneAlipay(PayoneMethod):
    method = "alipay"
    verbose_name = _("Alipay via PAYONE")
    public_name = _("Alipay")
    clearingtype = "wlt"
    wallettype = "ALP"

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return False


class PayonePaydirekt(PayoneMethod):
    method = "paydirekt"
    verbose_name = _("paydirekt via PAYONE")
    public_name = _("paydirekt")
    clearingtype = "wlt"
    wallettype = "PDT"

    def _get_payment_params(self, request, payment):
        d = super()._get_payment_params(request, payment)
        d["add_paydata[shopping_cart_type]"] = "DIGITAL"
        d["email"] = payment.order.email
        # TODO some kind of "workorder" required?
        return d


class PayonePaysafecard(PayoneMethod):
    method = "paysafecard"
    verbose_name = _("paysafeard via PAYONE")
    public_name = _("paysafecard")
    clearingtype = "wlt"
    wallettype = "PSC"


class PayoneQiwi(PayoneMethod):
    method = "qiwi"
    verbose_name = _("Qiwi via PAYONE")
    public_name = _("Qiwi")
    clearingtype = "wlt"
    wallettype = "QIW"


"""
Test status:

CC: works
CC 3DS: works
eps: works
giropay: works (although only in live mode)
SOFORT: works
SEPA DEBIT: unimplemented
PayPal: works
ideal: works
przelewy24: untested (not configured)
bancontact: untested (not configured)
alipay: untested (not configured)
paydirekt: untested (not configured)
multibanco: untested (not configured)
bct: untested (not configured)
mybank: untested (not configured)
paysafecard: untested (not configured)
qiwi: untested (not configured)
"""
