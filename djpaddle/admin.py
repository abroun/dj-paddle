from django.contrib import admin
import json

from . import models

admin.site.register(models.Checkout)


class PriceInline(admin.TabularInline):
    model = models.Price


@admin.register(models.Plan)
class PlanAdmin(admin.ModelAdmin):
    inlines = (PriceInline,)
    list_display = (
        "id",
        "name",
    )


@admin.register(models.Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "subscriber",
        "email",
        "status",
        "plan",
    )
    list_filter = (
        "status",
        "plan",
    )

@admin.register(models.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "base_price",
        "sale_price",
        "currency"
    )

@admin.register(models.ProductPurchase)
class ProductPurchaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "product",
        "product_name",
        "quantity"
    )

    def product_name(self, obj):
        return obj.product.name

@admin.register(models.WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ("time", "alert_name")

    def alert_name(self, instance):
        return instance.payload.get("alert_name")


@admin.register(models.ReplayedEvent)
class ReplayedEventAdmin(WebhookEventAdmin):
    pass