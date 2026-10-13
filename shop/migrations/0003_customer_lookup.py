import django.db.models.deletion
from django.db import migrations, models


def fill_phone_keys(apps, schema_editor):
    from shop.models import phone_key

    Customer = apps.get_model('shop', 'Customer')
    for customer in Customer.objects.all():
        customer.phone_key = phone_key(customer.phone)
        customer.save(update_fields=['phone_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0002_order_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='phone_key',
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=20),
        ),
        migrations.RunPython(fill_phone_keys, migrations.RunPython.noop),
        migrations.CreateModel(
            name='CustomerLookupCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('used', models.BooleanField(default=False)),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='lookup_codes', to='shop.customer',
                )),
            ],
            options={'ordering': ('-created_at',)},
        ),
    ]
