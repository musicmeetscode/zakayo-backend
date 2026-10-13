import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from shop.models import Category, Product

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'sample_catalogue.json'


class Command(BaseCommand):
    help = (
        'Load the SAMPLE catalogue (placeholder names, prices and regulatory marks) for development. '
        'Existing products are matched by SKU and updated.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(FIXTURE), help='Path to a catalogue JSON file.')

    @transaction.atomic
    def handle(self, *args, **options):
        data = json.loads(Path(options['file']).read_text())

        categories = {}
        for row in data['categories']:
            slug = row.pop('slug')
            categories[slug], _ = Category.objects.update_or_create(slug=slug, defaults=row)

        created = updated = 0
        for row in data['products']:
            row['category'] = categories[row['category']]
            sku = row.pop('sku')
            _, was_created = Product.objects.update_or_create(sku=sku, defaults=row)
            created += was_created
            updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f'{len(categories)} categories; {created} products created, {updated} updated.'
        ))
        self.stdout.write(self.style.WARNING('This is placeholder data — replace it with the real product register.'))
