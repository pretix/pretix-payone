import hashlib
import json
import logging
import requests
from decimal import Decimal
from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.base.models import Order, OrderPayment, Quota
from pretix.base.payment import PaymentException
from pretix.base.services.locking import LockTimeoutException
from pretix.multidomain.urlreverse import eventreverse
from pretix_mollie.utils import refresh_mollie_token
from requests import HTTPError

logger = logging.getLogger(__name__)


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    signer = signing.Signer(salt="safe-redirect")
    try:
        url = signer.unsign(request.GET.get("url", ""))
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid parameter")

    r = render(
        request,
        "pretix_payone/redirect.html",
        {
            "url": url,
        },
    )
    r._csp_ignore = True
    return r


class PayoneOrderView:
    def dispatch(self, request, *args, **kwargs):
        try:
            self.order = request.event.orders.get(code=kwargs["order"])
            if (
                hashlib.sha1(self.order.secret.lower().encode()).hexdigest()
                != kwargs["hash"].lower()
            ):
                raise Http404("")
        except Order.DoesNotExist:
            # Do a hash comparison as well to harden timing attacks
            if (
                "abcdefghijklmnopq".lower()
                == hashlib.sha1("abcdefghijklmnopq".encode()).hexdigest()
            ):
                raise Http404("")
            else:
                raise Http404("")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def payment(self):
        return get_object_or_404(
            self.order.payments,
            pk=self.kwargs["payment"],
            provider__startswith="payone",
        )

    @cached_property
    def pprov(self):
        return self.payment.payment_provider


@method_decorator(xframe_options_exempt, "dispatch")
class ReturnView(PayoneOrderView, View):
    def get(self, request, *args, **kwargs):
        # TODO
        ...

    def _redirect_to_order(self):
        if self.request.session.get("payment_payone_order_secret") != self.order.secret:
            messages.error(
                self.request,
                _(
                    "Sorry, there was an error in the payment process. Please check the link "
                    "in your emails to continue."
                ),
            )
            return redirect(eventreverse(self.request.event, "presale:event.index"))

        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )


@method_decorator(csrf_exempt, "dispatch")
class WebhookView(View):
    def post(self, request, *args, **kwargs):
        ...  # todo
        return HttpResponse(status=200)

    @cached_property
    def payment(self):
        return get_object_or_404(
            OrderPayment.objects.filter(order__event=self.request.event),
            pk=self.kwargs["payment"],
            provider__startswith="payone",
        )
