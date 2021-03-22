import logging
from django.dispatch import receiver
from django.urls import resolve
from django.utils.translation import gettext_lazy as _

from pretix.base.middleware import _parse_csp, _merge_csp, _render_csp
from pretix.base.settings import settings_hierarkey
from pretix.base.signals import logentry_display, register_payment_providers
from pretix.presale.signals import process_response

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_payone")
def register_payment_provider(sender, **kwargs):
    from .payment import (
        PayoneCC,
        PayoneGiropay,
        PayonePayPal,
        PayoneSEPADebit,
        PayoneSettingsHolder,
    )

    return [
        PayonePayPal,
        PayoneGiropay,
        PayoneSEPADebit,
        PayoneCC,
        PayoneSettingsHolder,
    ]


@receiver(signal=logentry_display, dispatch_uid="payone_logentry_display")
def pretixcontrol_logentry_display(sender, logentry, **kwargs):
    if not logentry.action_type.startswith("pretix_payone.event"):
        return

    # TODO
    plains = {
        "canceled": _("Payment canceled."),
        "failed": _("Payment failed."),
        "paid": _("Payment succeeded."),
        "expired": _("Payment expired."),
        "disabled": _(
            "Payment method disabled since we were unable to refresh the access token. Please "
            "contact support."
        ),
    }
    text = plains.get(logentry.action_type[20:], None)
    if text:
        return _("PAYONE reported an event: {}").format(text)


@receiver(signal=process_response, dispatch_uid="payment_payone_middleware_resp")
def signal_process_response(sender, request, response, **kwargs):
    from .payment import PayoneSettingsHolder
    provider = PayoneSettingsHolder(sender)
    url = resolve(request.path_info)

    if provider.settings.get('_enabled', as_type=bool) and ("checkout" in url.url_name or "order.pay" in url.url_name):
        if 'Content-Security-Policy' in response:
            h = _parse_csp(response['Content-Security-Policy'])
        else:
            h = {}

        sources = ['frame-src', 'style-src', 'script-src', 'img-src', 'connect-src']

        csps = {src: ['https://secure.pay1.de'] for src in sources}

        _merge_csp(h, csps)

        if h:
            response['Content-Security-Policy'] = _render_csp(h)
    return response


settings_hierarkey.add_default("payment_payone_method_creditcard", True, bool)
