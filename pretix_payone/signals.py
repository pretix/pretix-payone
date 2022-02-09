import logging
from django.dispatch import receiver
from django.urls import resolve
from django.utils.translation import gettext_lazy as _
from pretix.base.middleware import _merge_csp, _parse_csp, _render_csp
from pretix.base.signals import logentry_display, register_payment_providers
from pretix.presale.signals import process_response

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_payone")
def register_payment_provider(sender, **kwargs):
    from .payment import (
        PayoneAlipay,
        PayoneBancontact,
        PayoneCC,
        PayoneEPS,
        PayoneGiropay,
        PayoneIdeal,
        PayoneMultibanco,
        PayoneMyBank,
        PayonePaydirekt,
        PayonePayPal,
        PayonePaysafecard,
        PayonePrzelewy24,
        PayoneQiwi,
        PayoneSettingsHolder,
        PayoneSofort,
        PayoneVerkkopankki,
    )

    return [
        PayoneCC,
        PayoneEPS,
        PayoneGiropay,
        PayonePayPal,
        PayonePaysafecard,
        PayoneSettingsHolder,
        PayonePaydirekt,
        PayoneQiwi,
        PayoneIdeal,
        PayoneAlipay,
        PayoneBancontact,
        PayoneMultibanco,
        PayoneVerkkopankki,
        PayoneMyBank,
        PayonePrzelewy24,
        PayoneSofort,
    ]


@receiver(signal=logentry_display, dispatch_uid="payone_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_payone.event"):
        return

    text = logentry.action_type[20:]
    if text:
        return _("PAYONE reported an event: {}").format(text)


@receiver(signal=process_response, dispatch_uid="payment_payone_middleware_resp")
def signal_process_response(sender, request, response, **kwargs):
    from .payment import PayoneSettingsHolder

    provider = PayoneSettingsHolder(sender)
    url = resolve(request.path_info)

    if provider.settings.get("_enabled", as_type=bool) and (
        "checkout" in url.url_name or "order.pay" in url.url_name
    ):
        if "Content-Security-Policy" in response:
            h = _parse_csp(response["Content-Security-Policy"])
        else:
            h = {}

        sources = ["frame-src", "style-src", "script-src", "img-src", "connect-src"]

        csps = {src: ["https://secure.pay1.de"] for src in sources}

        _merge_csp(h, csps)

        if h:
            response["Content-Security-Policy"] = _render_csp(h)
    return response
