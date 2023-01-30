import hashlib
import json
import logging
import urllib
from decimal import Decimal
from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.db.models import Sum
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment, OrderRefund, Quota
from pretix.helpers import OF_SELF
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse

from pretix_payone.models import ReferencedPayoneObject

logger = logging.getLogger(__name__)


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    try:
        data = signing.loads(request.GET.get("data", ""), salt="safe-redirect")
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid parameter")

    if "go" in request.GET:
        if "session" in data:
            for k, v in data["session"].items():
                request.session[k] = v
        return redirect(data["url"])
    else:
        params = request.GET.copy()
        params["go"] = "1"
        r = render(
            request,
            "pretix_payone/redirect.html",
            {
                "url": build_absolute_uri(
                    request.event, "plugins:pretix_payone:redirect"
                )
                + "?"
                + urllib.parse.urlencode(params),
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
        if kwargs["action"] == "error":
            with transaction.atomic():
                p = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=self.payment.pk)
                if p.state == OrderPayment.PAYMENT_STATE_CREATED:
                    self.payment.fail()
            messages.error(
                self.request,
                _("The payment process has failed. See below for more information."),
            )
            return self._redirect_to_order()
        elif kwargs["action"] == "cancel":
            with transaction.atomic():
                p = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=self.payment.pk)
                if p.state == OrderPayment.PAYMENT_STATE_CREATED:
                    self.payment.state = OrderPayment.PAYMENT_STATE_CANCELED
                    self.payment.save(update_fields=["state"])
                    self.payment.order.log_action(
                        "pretix.event.order.payment.canceled",
                        {
                            "local_id": self.payment.local_id,
                            "provider": self.payment.provider,
                            "data": self.payment.info_data,
                        },
                    )
            return self._redirect_to_order()
        elif kwargs["action"] == "success":
            with transaction.atomic():
                p = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=self.payment.pk)
                if p.state == OrderPayment.PAYMENT_STATE_CREATED:
                    p.state = OrderPayment.PAYMENT_STATE_PENDING
                    p.save(update_fields=["state"])
            return self._redirect_to_order()

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
    @scopes_disabled()
    def post(self, request, *args, **kwargs):
        try:
            r = ReferencedPayoneObject.objects.get(txid=request.POST.get("txid"))
        except ReferencedPayoneObject.DoesNotExist:
            return HttpResponse(status=409)

        pprov = r.payment.payment_provider
        if hashlib.md5(pprov.settings.key.encode()).hexdigest() != request.POST.get(
            "key"
        ):
            return HttpResponse("Invalid key", status=403)

        if pprov.settings.aid != request.POST.get("aid"):
            return HttpResponse("Invalid AID", status=403)

        if pprov.settings.portalid != request.POST.get("portalid"):
            return HttpResponse("Invalid Portal ID", status=403)

        if request.POST.get("mode") == "test" and not r.order.testmode:
            return HttpResponse("Invalid testmode usage", status=403)

        data = {k: request.POST.get(k) for k in request.POST.keys() if k not in ("key")}

        r.order.log_action(
            f'pretix_payone.event.{data["txaction"]}',
            data={
                "local_id": r.payment.local_id,
                "provider": r.payment.provider,
                "data": data,
            },
        )
        balance = None
        if "balance" in data:
            balance = Decimal(data["balance"])

        if "sequencenumber" in data:
            d = r.payment.info_data
            d["sequencenumber"] = data["sequencenumber"]
            r.payment.info_data = d
            r.payment.save()

        if data["txaction"] in ("capture", "paid", "appointed"):
            is_paid = (
                (data["txaction"] == "appointed" and pprov.consider_appointed_as_paid)
                or balance is not None
                and balance <= Decimal("0.00")
            )
            if (
                r.payment.state
                not in (
                    OrderPayment.PAYMENT_STATE_CONFIRMED,
                    OrderPayment.PAYMENT_STATE_REFUNDED,
                )
                and is_paid
            ):
                try:
                    r.payment.confirm()
                except Quota.QuotaExceededException:
                    pass
        elif data["txaction"] in ("refund", "cancelation"):
            existing_refund_amount = r.payment.refunds.exclude(
                state__in=(
                    OrderRefund.REFUND_STATE_CANCELED,
                    OrderRefund.REFUND_STATE_FAILED,
                )
            ).aggregate(a=Sum("amount"))["a"] or Decimal("0.00")
            new_refund_amount = r.payment.amount - Decimal(data["receivable"])
            if new_refund_amount > existing_refund_amount:
                r.payment.create_external_refund(
                    new_refund_amount - existing_refund_amount, info=json.dumps(data)
                )

        return HttpResponse("TSOK", status=200)

    @cached_property
    def payment(self):
        return get_object_or_404(
            OrderPayment.objects.filter(order__event=self.request.event),
            pk=self.kwargs["payment"],
            provider__startswith="payone",
        )
