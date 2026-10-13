from django.db import transaction
from rest_framework import serializers

from .models import (
    COUNTRY_CHOICES, CUSTOMER_TYPE_CHOICES, Category, ContactMessage, Customer, NewsletterSubscriber, Order,
    OrderItem, Product, QuoteRequest,
)

PHONE_REGEX = r'^\+?[0-9 ()-]{9,20}$'


class CategorySerializer(serializers.ModelSerializer):
    id = serializers.CharField(source='slug')

    class Meta:
        model = Category
        fields = ('id', 'label', 'description')


class ProductSerializer(serializers.ModelSerializer):
    category = serializers.SlugRelatedField(slug_field='slug', read_only=True)

    class Meta:
        model = Product
        fields = (
            'id', 'slug', 'sku', 'name', 'category', 'short_description', 'description', 'price_ugx', 'unit',
            'min_order_qty', 'regulatory', 'in_stock', 'quote_only', 'featured', 'image', 'visual', 'specs',
        )


# --- Orders -----------------------------------------------------------------

class OrderCustomerSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150, min_length=2)
    email = serializers.EmailField()
    phone = serializers.RegexField(PHONE_REGEX, error_messages={'invalid': 'Enter a valid phone number.'})
    organization = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    customer_type = serializers.ChoiceField(choices=CUSTOMER_TYPE_CHOICES)


class OrderDeliverySerializer(serializers.Serializer):
    country = serializers.ChoiceField(choices=COUNTRY_CHOICES)
    region = serializers.CharField(max_length=100, min_length=2)
    city = serializers.CharField(max_length=100, min_length=2)
    address = serializers.CharField(max_length=255, min_length=4)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class OrderLineSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    sku = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.IntegerField(min_value=1, max_value=100_000)
    # Accepted for compatibility but ignored: prices always come from the database.
    unit_price_ugx = serializers.IntegerField(required=False)


class OrderCreateSerializer(serializers.Serializer):
    """Mirrors the frontend `OrderPayload`. Prices are re-read from the catalogue; client totals are ignored."""

    customer = OrderCustomerSerializer()
    delivery = OrderDeliverySerializer()
    payment_method = serializers.ChoiceField(choices=Order.PAYMENT_CHOICES)
    currency = serializers.ChoiceField(choices=[('UGX', 'UGX'), ('USD', 'USD')], default='UGX')
    items = OrderLineSerializer(many=True, allow_empty=False)
    subtotal_ugx = serializers.IntegerField(required=False)

    def validate_items(self, lines):
        ids = [line['product_id'] for line in lines]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('Each product may appear only once.')
        products = Product.objects.published().in_bulk(ids)
        errors = []
        for line in lines:
            product = products.get(line['product_id'])
            if product is None:
                errors.append(f"Product {line['product_id']} is no longer available.")
            elif product.quote_only:
                errors.append(f'{product.name} is sold by quotation only.')
            elif not product.in_stock:
                errors.append(f'{product.name} is out of stock.')
            elif line['quantity'] < product.min_order_qty:
                errors.append(f'{product.name} has a minimum order of {product.min_order_qty}.')
            else:
                line['product'] = product
        if errors:
            raise serializers.ValidationError(errors)
        return lines

    @transaction.atomic
    def create(self, validated_data):
        c, d = validated_data['customer'], validated_data['delivery']
        customer = Customer.upsert(
            c['email'],
            full_name=c['full_name'], phone=c['phone'], organization=c['organization'],
            customer_type=c['customer_type'], country=d['country'], region=d['region'],
            city=d['city'], address=d['address'],
        )
        order = Order.objects.create(
            customer=customer,
            full_name=c['full_name'], email=c['email'].lower(), phone=c['phone'], organization=c['organization'],
            customer_type=c['customer_type'],
            country=d['country'], region=d['region'], city=d['city'], address=d['address'], notes=d['notes'],
            payment_method=validated_data['payment_method'], display_currency=validated_data['currency'],
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order, product=line['product'], product_name=line['product'].name, sku=line['product'].sku,
                unit=line['product'].unit, unit_price_ugx=line['product'].price_ugx, quantity=line['quantity'],
            )
            for line in validated_data['items']
        ])
        order.recalculate_subtotal()
        order.save(update_fields=['subtotal_ugx'])
        return order

    def to_representation(self, order):
        return {
            'reference': order.reference,
            'status': order.status,
            'subtotal_ugx': order.subtotal_ugx,
            'created_at': order.created_at.isoformat(),
            'customer_email': order.email,
            'tracking_token': order.tracking_token,
        }


class TrackedOrderItemSerializer(serializers.ModelSerializer):
    line_total_ugx = serializers.IntegerField(read_only=True)

    class Meta:
        model = OrderItem
        fields = ('product_name', 'sku', 'unit', 'quantity', 'unit_price_ugx', 'line_total_ugx')


class TrackedOrderSerializer(serializers.ModelSerializer):
    """What a customer sees on the tracking page (opened from the QR code or an email link)."""

    items = TrackedOrderItemSerializer(many=True, read_only=True)
    status_label = serializers.CharField(source='get_status_display')
    country_name = serializers.CharField(source='get_country_display')
    payment_method_label = serializers.CharField(source='get_payment_method_display')
    total_ugx = serializers.IntegerField()
    can_confirm_delivery = serializers.BooleanField()

    class Meta:
        model = Order
        fields = (
            'reference', 'status', 'status_label', 'full_name', 'organization', 'city', 'region', 'country_name',
            'address', 'payment_method_label', 'items', 'subtotal_ugx', 'delivery_fee_ugx', 'total_ugx',
            'created_at', 'confirmed_at', 'dispatched_at', 'delivered_at', 'received_by', 'can_confirm_delivery',
        )


class ConfirmDeliverySerializer(serializers.Serializer):
    token = serializers.CharField()
    received_by = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    feedback = serializers.CharField(max_length=1000, required=False, allow_blank=True, default='')


# --- Quotes, contact, newsletter --------------------------------------------

class QuoteRequestSerializer(serializers.ModelSerializer):
    product_id = serializers.PrimaryKeyRelatedField(source='product', queryset=Product.objects.published())
    phone = serializers.RegexField(PHONE_REGEX, error_messages={'invalid': 'Enter a valid phone number.'})

    class Meta:
        model = QuoteRequest
        fields = ('id', 'product_id', 'sku', 'quantity', 'full_name', 'email', 'phone', 'organization', 'country', 'message')
        read_only_fields = ('id',)
        extra_kwargs = {'sku': {'required': False}, 'quantity': {'min_value': 1}}

    def create(self, validated_data):
        product = validated_data['product']
        validated_data['sku'] = product.sku
        validated_data['email'] = validated_data['email'].lower()
        validated_data['customer'] = Customer.upsert(
            validated_data['email'], full_name=validated_data['full_name'], phone=validated_data['phone'],
            organization=validated_data['organization'], country=validated_data['country'],
        )
        return super().create(validated_data)

    def to_representation(self, instance):
        return {'id': instance.pk}


class ContactMessageSerializer(serializers.ModelSerializer):
    phone = serializers.RegexField(
        PHONE_REGEX, required=False, allow_blank=True, error_messages={'invalid': 'Enter a valid phone number.'},
    )

    class Meta:
        model = ContactMessage
        fields = ('id', 'full_name', 'email', 'phone', 'organization', 'topic', 'subject', 'message')
        read_only_fields = ('id',)
        extra_kwargs = {'message': {'min_length': 10, 'max_length': 3000}, 'subject': {'min_length': 3}}

    def to_representation(self, instance):
        return {'id': instance.pk}


class NewsletterSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def create(self, validated_data):
        subscriber, _ = NewsletterSubscriber.objects.update_or_create(
            email=validated_data['email'].lower(), defaults={'is_active': True},
        )
        return subscriber

    def to_representation(self, instance):
        return {'email': instance.email}


# --- Returning-customer lookup ----------------------------------------------

class CustomerLookupSerializer(serializers.Serializer):
    phone = serializers.RegexField(PHONE_REGEX, error_messages={'invalid': 'Enter a valid phone number.'})


class CustomerVerifySerializer(CustomerLookupSerializer):
    code = serializers.RegexField(r'^\s*\d{6}\s*$', error_messages={'invalid': 'Enter the 6-digit code.'})


class SavedCustomerSerializer(serializers.ModelSerializer):
    """Details returned to pre-fill checkout, only after the emailed code is verified."""

    class Meta:
        model = Customer
        fields = ('full_name', 'email', 'phone', 'organization', 'customer_type', 'country', 'region', 'city', 'address')
