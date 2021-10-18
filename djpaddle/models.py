import logging
from datetime import datetime

from django.db import models
from django.conf import settings as djsettings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from paddle import PaddleClient

from . import settings, signals, mappers
from .fields import PaddleCurrencyCodeField
from .utils import PADDLE_DATETIME_FORMAT, PADDLE_DATE_FORMAT

log = logging.getLogger("djpaddle")

paddle_client = PaddleClient(
    vendor_id=settings.DJPADDLE_VENDOR_ID,
    api_key=settings.DJPADDLE_API_KEY,
    sandbox=settings.DJPADDLE_SANDBOX,
)


class PaddleBaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Plan(PaddleBaseModel):
    """
    'Plan' represents a Paddle subscription plan.
    """

    PADDLE_URI_LIST = "subscription/plans"

    BILLING_TYPE_DAY = "day"
    BILLING_TYPE_MONTH = "month"
    BILLING_TYPE_YEAR = "year"
    BILLING_TYPE = (
        (BILLING_TYPE_DAY, _("day")),
        (BILLING_TYPE_MONTH, _("month")),
        (BILLING_TYPE_YEAR, _("year")),
    )

    name = models.CharField(max_length=255)
    billing_type = models.CharField(choices=BILLING_TYPE, max_length=255)
    billing_period = models.IntegerField()
    trial_days = models.IntegerField(default=0)

    class Meta:
        ordering = ["created_at"]

    @classmethod
    def api_list(cls):
        return paddle_client.list_plans()

    @classmethod
    def api_get(cls, plan_id):
        plan_data = paddle_client.list_plans(plan=int(plan_id))[0]
        return plan_data

    @classmethod
    def sync_from_paddle_data(cls, data):
        pk = data.pop("id")
        initial_price = data.pop("initial_price", {})
        recurring_price = data.pop("recurring_price", {})

        plan, __ = cls.objects.get_or_create(pk=pk, defaults=data)

        # drop all existing prices and recreate them
        plan.prices.all().delete()
        prices = []
        for currency, quantity in initial_price.items():
            price = Price(
                plan=plan,
                currency=currency,
                quantity=float(quantity),
                recurring=False,
            )
            prices.append(price)
        for currency, quantity in recurring_price.items():
            price = Price(
                plan=plan,
                currency=currency,
                quantity=float(quantity),
                recurring=True,
            )
            prices.append(price)

        Price.objects.bulk_create(prices)

        return plan

    def __str__(self):
        return "{}:{}".format(self.name, self.id)


class Price(PaddleBaseModel):
    plan = models.ForeignKey("djpaddle.Plan", on_delete=models.CASCADE, related_name="prices")
    currency = PaddleCurrencyCodeField()
    quantity = models.FloatField()
    recurring = models.BooleanField()

    def __str__(self):
        return "{} {}".format(self.quantity, self.currency)

    class Meta:
        ordering = ["currency", "recurring"]
        unique_together = ("plan", "currency", "recurring")


class Subscription(PaddleBaseModel):
    """
    'Subscription' represents a Paddle subscription.

    They are automatically updated on behalf of the webhook calls
    'subscription_created', 'subscription_updated' and 'subscription_cancelled'.

    Stale subscriptions that are not associated with a subscriber are being
    linked by a customizable getter function. Linking is optional and can be configured through the
    settings ('DJPADDLE_LINK_STALE_SUBSCRIPTIONS').
    """

    STATUS_ACTIVE = "active"
    STATUS_TRIALING = "trialing"
    STATUS_PAST_DUE = "past_due"
    STATUS_PAUSED = "paused"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = (
        (STATUS_ACTIVE, _("active")),
        (STATUS_TRIALING, _("trialing")),
        (STATUS_PAST_DUE, _("past due")),
        (STATUS_PAUSED, _("paused")),
        (STATUS_DELETED, _("deleted")),
    )

    id = models.CharField(max_length=64, primary_key=True)
    subscriber = models.ForeignKey(
        settings.DJPADDLE_SUBSCRIBER_MODEL,
        related_name="subscriptions",
        null=True,
        default=None,
        on_delete=models.CASCADE,
    )

    cancel_url = models.URLField()
    checkout_id = models.CharField(max_length=64)
    currency = models.CharField(max_length=3)
    email = models.EmailField()
    event_time = models.DateTimeField()
    marketing_consent = models.BooleanField()
    next_bill_date = models.DateTimeField()
    passthrough = models.TextField()
    quantity = models.IntegerField()
    source = models.URLField()
    status = models.CharField(choices=STATUS_CHOICES, max_length=16)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE)
    unit_price = models.FloatField()
    update_url = models.URLField()

    class Meta:
        ordering = ["created_at"]

    @classmethod
    def _sanitize_webhook_payload(cls, payload):
        data = {}
        data["id"] = payload.pop("subscription_id")

        Subscriber = settings.get_subscriber_model()
        try:
            data["subscriber"] = mappers.get_subscriber_by_payload(Subscriber, payload)
        except Subscriber.DoesNotExist:
            warning = "Subscriber could not be found for subscription {0} with payload {1}. Subscriber left empty."
            log.warning(warning.format(data["id"], payload))
            data["subscriber"] = None

        # transform `subscription_plan_id` to plan ref
        plan_id = payload.pop("subscription_plan_id")
        try:
            data["plan"] = Plan.objects.get(pk=plan_id)
        except Plan.DoesNotExist:
            plan_data = Plan.api_get(plan_id=plan_id)
            data["plan"] = Plan.sync_from_paddle_data(plan_data)

        # sanitize fields
        valid_field_names = [field.name for field in cls._meta.get_fields()]
        for key, value in payload.items():
            # e.g. 'new_status' --> 'status'
            if key.startswith("new_"):
                key = key[4:]

            if key in valid_field_names:
                data[key] = value

        data = convert_datetime_strings_to_datetimes(data, cls)

        return data

    @classmethod
    def create_or_update_by_payload(cls, payload):
        data = cls._sanitize_webhook_payload(payload)
        pk = data.get("id")
        alert_name = payload.get("alert_name")
        try:
            subscription = cls.objects.get(pk=pk)
        except cls.DoesNotExist:
            subscription = cls.objects.create(pk=pk, **data)
            mappers.process_subscription_webhook_callback(alert_name, subscription.id)
            return subscription

        # NOTE: Some webhook events can come through with the same time (e.g. subscription_created
        # and subscription_payment_succeeded, so depending on ordering the < check below may
        # cause some data to be missed. One option is to change the check to be <= which may cause
        # oddness during event replays. The other option is to configure Paddle to only send
        # subscription_created events and not subscription_payment_succeeded events.
        if subscription.event_time < data["event_time"]:
            cls.objects.filter(pk=pk).update(**data)
            mappers.process_subscription_webhook_callback(alert_name, subscription.id)

    def __str__(self):
        return "{}:{}".format(self.subscriber, self.id)


class Checkout(models.Model):
    """
    Used to store checkout info from PaddleJS. Transient model which acts as
    a backup in case the webhook is not recieved straight away
    """

    id = models.CharField(max_length=64, primary_key=True)
    completed = models.BooleanField(null=True)
    passthrough = models.TextField(null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)


@receiver(signals.subscription_created)
@receiver(signals.subscription_updated)
@receiver(signals.subscription_cancelled)
@receiver(signals.subscription_payment_succeeded)
def subscription_event(sender, payload, *args, **kwargs):
    Subscription.create_or_update_by_payload(payload)


if settings.DJPADDLE_LINK_STALE_SUBSCRIPTIONS:

    @receiver(post_save, sender=settings.DJPADDLE_SUBSCRIBER_MODEL)
    def link_stale_subscriptions_to_subscriber(sender, instance, created, *args, **kwargs):
        if created:
            queryset = Subscription.objects.filter(subscriber=None)
            queryset = mappers.get_subscriptions_by_subscriber(instance, queryset)
            queryset.update(subscriber=instance)

class Product(PaddleBaseModel):
    """
    'Product' represents a Paddle product that can be bought with a one time purchase
    """

    name = models.CharField(max_length=255)
    description = models.CharField(max_length=1024, null=True)

    base_price = models.FloatField()
    sale_price = models.FloatField(null=True)
    currency = PaddleCurrencyCodeField()
    icon = models.CharField(max_length=1024, null=True)

    @classmethod
    def api_list(cls):
        return paddle_client.list_products()["products"]

    @classmethod
    def api_get(cls, product_id):
        # Paddle API doesn't let you filter by product id so we need to
        # get all products and filter ourselves
        products = cls.api_list()
        return next((p for p in products if p["id"] == product_id), None)

    @classmethod
    def sync_from_paddle_data(cls, data):
        pk = data.pop("id")

        # Currently 'screenshots' is empty in the Paddle API doc samples, and
        # there doesn't seem to be a way to set screenshots in the Paddle 
        # dashboard, so we ignore it for now
        _ = data.pop("screenshots")

        product, __ = cls.objects.get_or_create(pk=pk, defaults=data)

        return product

    def __str__(self):
        return "{}:{}".format(self.name, self.id)

def convert_string_to_datetime(datetime_str):

    result = datetime_str

    try:
        result = datetime.strptime(result, PADDLE_DATETIME_FORMAT)
    except ValueError:
        # Some fields such as next_bill_date should perhaps be
        # a DateField not DatetimeField
        result = datetime.strptime(result, PADDLE_DATE_FORMAT)

    if djsettings.USE_TZ:
        local_time_zone = timezone.get_default_timezone()
        result = timezone.make_aware(result, local_time_zone)

    return result

def convert_datetime_strings_to_datetimes(data, model):
    datetime_fields = [field.name for field in model._meta.get_fields() if isinstance(field, models.DateTimeField)]
    for field in datetime_fields:
        if field not in data:
            continue

        data[field] = convert_string_to_datetime(data[field])

    return data

# Table to keep track of received webhooks. Retention period is configurable
class WebhookEvent(models.Model):
    time = models.DateTimeField()
    payload = models.JSONField()

# For people paranoid about missing webhooks it's possible to setup polling to periodically
# grab all sent events.
class ReplayedEvent(models.Model):
    time = models.DateTimeField()
    payload = models.JSONField()