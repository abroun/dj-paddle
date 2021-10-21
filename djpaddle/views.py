from distutils.util import strtobool

from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from django.views.generic.edit import BaseCreateView
from datetime import datetime, timezone, timedelta
from django.contrib.auth.models import AnonymousUser

from . import signals, settings
from .models import Checkout, convert_datetime_strings_to_datetimes, \
    convert_string_to_datetime, WebhookEvent
from .utils import is_valid_webhook


@method_decorator(csrf_exempt, name="dispatch")
class PaddleWebhookView(View):
    SUPPORTED_WEBHOOKS = (
        # Subscription Alerts
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_payment_succeeded",
        "subscription_payment_failed",
        "subscription_payment_refunded",
        # One-off Purchases
        "locker_processed",
        "payment_succeeded",
        "payment_refunded",
        # Risk & Dispute Alerts
        "payment_dispute_created",
        "payment_dispute_closed",
        "high_risk_transaction_created",
        "high_risk_transaction_updated",
        # Payout Alerts
        "transfer_created",
        "transfer_paid",
        # Audience Alerts
        "new_audience_member",
        "update_audience_member",
    )

    def post(self, request, *args, **kwargs):
        """
        handle paddle webhook requests by
        - validating the payload signature
        - sending a django signal for each of the SUPPORTED_WEBHOOKS
        """
        payload = request.POST.dict()

        if not is_valid_webhook(payload):
            return HttpResponseBadRequest("webhook validation failed")

        alert_name = payload.get("alert_name")
        if not alert_name:
            return HttpResponseBadRequest("'alert_name' missing")

        if alert_name in self.SUPPORTED_WEBHOOKS:

            # Store the webhook if needed
            if settings.DJPADDLE_WEBHOOK_RETENTION_DAYS > 0:

                event_time = payload.get("event_time")
                if not event_time:
                    return HttpResponseBadRequest("'event_time' missing")

                # Clean out old events
                oldest_event_time = datetime.now(timezone.utc) - timedelta(days=settings.DJPADDLE_WEBHOOK_RETENTION_DAYS)
                WebhookEvent.objects.filter(time__lt=oldest_event_time).delete()

                event = WebhookEvent(time=convert_string_to_datetime(event_time), payload=payload)
                event.save()

            signal = getattr(signals, alert_name)
            if signal:  # pragma: no cover
                signal.send(sender=self.__class__, payload=payload)

        return HttpResponse()

class PaddlePostCheckoutApiView(BaseCreateView):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        data = request.POST.dict()
        redirect_url = data.pop("redirect_url") if "redirect_url" in data else ""
        pk = data.pop("id")
        if not pk:
            return HttpResponseBadRequest('Missing "id"')
        try:
            data["completed"] = bool(strtobool(data["completed"]))
        except (KeyError, ValueError):
            return HttpResponseBadRequest('Missing "completed"')

        if request.user.is_authenticated:
            # Beholder Vision specific validation
            email = data.get("email")
            passthrough = data.get("passthrough")

            if email != request.user.email or passthrough != request.user.username:
                return HttpResponseBadRequest("Checkout not from current user")

        try:
            data = convert_datetime_strings_to_datetimes(data, Checkout)
        except ValueError as e:
            return HttpResponseBadRequest(e)

        Checkout.objects.update_or_create(pk=pk, defaults=data)

        next_url = request.GET.get("next")
        if next_url:
            next_url = "{0}?checkout={1}".format(next_url, pk)
            return JsonResponse({"redirect_url": next_url}, status=200)

        if redirect_url and redirect_url != "null":
            redirect_url = "{0}?checkout={1}".format(redirect_url, pk)
            return JsonResponse({"redirect_url": redirect_url}, status=200)

        return JsonResponse({}, status=204)


paddle_webhook_view = PaddleWebhookView.as_view()
post_checkout_api_view = PaddlePostCheckoutApiView.as_view()
