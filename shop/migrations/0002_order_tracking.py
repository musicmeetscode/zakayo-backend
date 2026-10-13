import secrets

from django.db import migrations, models

import shop.models


def fill_tokens(apps, schema_editor):
    # A callable default would give every existing row the same value, so fill them one by one.
    Order = apps.get_model('shop', 'Order')
    for order in Order.objects.filter(tracking_token__isnull=True):
        order.tracking_token = secrets.token_urlsafe(24)
        order.save(update_fields=['tracking_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='tracking_token',
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='order',
            name='tracking_token',
            field=models.CharField(
                default=shop.models.generate_tracking_token, editable=False, max_length=64, unique=True,
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='received_by',
            field=models.CharField(blank=True, help_text='Name given by the customer on delivery.', max_length=150),
        ),
        migrations.AddField(
            model_name='order',
            name='delivery_feedback',
            field=models.TextField(blank=True, help_text='Comment left by the customer on delivery.'),
        ),
    ]
