from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status, views, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from . import emails
from .models import Category, Customer, CustomerLookupCode, Order, Product
from .serializers import (
    CategorySerializer, ConfirmDeliverySerializer, ContactMessageSerializer, CustomerLookupSerializer,
    CustomerVerifySerializer, NewsletterSerializer, OrderCreateSerializer, ProductSerializer, QuoteRequestSerializer,
    SavedCustomerSerializer, TrackedOrderSerializer,
)


class CataloguePagination(PageNumberPagination):
    # The frontend loads the whole (small) catalogue in one request and filters client-side.
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """Published products. `GET /api/products/` and `GET /api/products/{slug}/`."""

    serializer_class = ProductSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    pagination_class = CataloguePagination
    lookup_field = 'slug'

    def get_queryset(self):
        return Product.objects.published().select_related('category')


class CategoryListView(generics.ListAPIView):
    serializer_class = CategorySerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    pagination_class = None

    def get_queryset(self):
        # Only categories with something to show, so the site never links to an empty page.
        return Category.objects.filter(products__in=Product.objects.published()).distinct()


class PublicCreateView(generics.CreateAPIView):
    """Anonymous form submission, rate-limited per IP. Subclasses send their emails in `after_create`."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = 'public_forms'

    def perform_create(self, serializer):
        instance = serializer.save()
        # Email only once the row is committed, and never let mail problems fail the request.
        transaction.on_commit(lambda: self.after_create(instance))

    def after_create(self, instance):
        pass


class OrderCreateView(PublicCreateView):
    """`POST /api/orders/` — place an order from the website checkout."""

    serializer_class = OrderCreateSerializer

    def after_create(self, order):
        emails.send_order_received(order)


class QuoteRequestCreateView(PublicCreateView):
    serializer_class = QuoteRequestSerializer

    def after_create(self, quote):
        emails.send_quote_received(quote)


class ContactCreateView(PublicCreateView):
    serializer_class = ContactMessageSerializer

    def after_create(self, message):
        emails.send_contact_received(message)


class NewsletterSubscribeView(PublicCreateView):
    serializer_class = NewsletterSerializer

    def create(self, request, *args, **kwargs):
        # Re-subscribing is fine and must not reveal whether an address was already on the list.
        super().create(request, *args, **kwargs)
        return Response({'message': 'Subscribed.'}, status=status.HTTP_201_CREATED)


class TrackedOrderMixin:
    """Look up an order by reference + secret tracking token. A wrong token looks exactly like a missing order."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = 'tracking'

    def get_order(self, reference, token):
        return get_object_or_404(
            Order.objects.prefetch_related('items'), reference=reference.upper(), tracking_token=token or '-',
        )


class OrderTrackingView(TrackedOrderMixin, views.APIView):
    """`GET /api/orders/{reference}/?token=...` — the customer's tracking page."""

    def get(self, request, reference):
        order = self.get_order(reference, request.query_params.get('token'))
        return Response(TrackedOrderSerializer(order).data)


class ConfirmDeliveryView(TrackedOrderMixin, views.APIView):
    """`POST /api/orders/{reference}/confirm-delivery/` — the customer confirms receipt (QR code on the delivery note)."""

    def post(self, request, reference):
        payload = ConfirmDeliverySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order = self.get_order(reference, payload.validated_data['token'])
        try:
            order.confirm_delivery(payload.validated_data['received_by'], payload.validated_data['feedback'])
        except DjangoValidationError as exc:
            raise ValidationError({'status': exc.messages}) from exc
        emails.send_delivery_confirmed(order)
        return Response(TrackedOrderSerializer(order).data)


class CustomerLookupView(views.APIView):
    """
    `POST /api/customers/lookup/` {phone} — returning customers at checkout.
    If the phone matches a customer, a 6-digit code is emailed to the address on file. Details are only
    released by the verify endpoint, so a phone number alone never exposes anyone's information.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = 'customer_lookup'

    def post(self, request):
        payload = CustomerLookupSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        customer = Customer.find_by_phone(payload.validated_data['phone'])
        if customer is None:
            return Response({'found': False, 'masked_email': ''})
        emails.send_lookup_code(customer, CustomerLookupCode.issue(customer))
        return Response({'found': True, 'masked_email': customer.masked_email})


class CustomerVerifyView(views.APIView):
    """`POST /api/customers/verify/` {phone, code} — returns saved details for checkout pre-fill."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_scope = 'customer_lookup'

    def post(self, request):
        payload = CustomerVerifySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        customer = Customer.find_by_phone(payload.validated_data['phone'])
        if customer is None or not CustomerLookupCode.verify(customer, payload.validated_data['code']):
            raise ValidationError({'code': ['That code is incorrect or has expired. Request a new one.']})
        return Response(SavedCustomerSerializer(customer).data)
