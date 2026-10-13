from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register('products', views.ProductViewSet, basename='product')

urlpatterns = [
    *router.urls,
    path('categories/', views.CategoryListView.as_view(), name='category-list'),
    path('orders/', views.OrderCreateView.as_view(), name='order-create'),
    path('orders/<str:reference>/', views.OrderTrackingView.as_view(), name='order-tracking'),
    path(
        'orders/<str:reference>/confirm-delivery/', views.ConfirmDeliveryView.as_view(), name='order-confirm-delivery',
    ),
    path('customers/lookup/', views.CustomerLookupView.as_view(), name='customer-lookup'),
    path('customers/verify/', views.CustomerVerifyView.as_view(), name='customer-verify'),
    path('quote-requests/', views.QuoteRequestCreateView.as_view(), name='quote-request-create'),
    path('contact/', views.ContactCreateView.as_view(), name='contact-create'),
    path('newsletter/', views.NewsletterSubscribeView.as_view(), name='newsletter-subscribe'),
]
