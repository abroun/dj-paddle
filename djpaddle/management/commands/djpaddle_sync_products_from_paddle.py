"""
sync_products_from_paddle command.
"""
from django.core.management.base import BaseCommand

from ...models import Plan, Product

class Command(BaseCommand):
    """Sync products from paddle."""

    help = "Sync products from paddle."

    def handle(self, *args, **options):
        """Call sync_from_paddle_data for each product returned by api_list."""

        for product_data in Product.api_list():
           product = Product.sync_from_paddle_data(product_data)
           print("Synchronized {0}".format(str(product)))
