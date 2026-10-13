from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from accounts.models import CustomUser
from shop.models import Category, ContactMessage, Customer, CustomerLookupCode, EmailLog, NewsletterSubscriber, Order, Product, QuoteRequest

STAFF_EMAILS = ['sales@zakayo.test']


def order_payload(**overrides):
    payload = {
        'customer': {
            'full_name': 'Amina Nakato', 'email': 'Amina@Clinic.ug', 'phone': '+256 700 000000',
            'organization': 'Kisoga Health Centre', 'customer_type': 'health_facility',
        },
        'delivery': {'country': 'UG', 'region': 'Mukono', 'city': 'Kisoga', 'address': 'Main road', 'notes': ''},
        'payment_method': 'mobile_money',
        'currency': 'UGX',
        'items': [],
        'subtotal_ugx': 1,
    }
    payload.update(overrides)
    return payload


@override_settings(SALES_NOTIFICATION_EMAILS=STAFF_EMAILS)
class ShopTestCase(APITestCase):
    def setUp(self):
        cache.clear()  # reset throttling between tests
        self.category = Category.objects.create(slug='rapid-tests', label='Rapid Tests')
        self.kit = Product.objects.create(
            name='HIV Test Kit', sku='KIT-25', category=self.category, short_description='Kit',
            price_ugx=275_000, unit='Kit of 25 tests',
        )
        self.lancets = Product.objects.create(
            name='Lancets', sku='LAN-200', category=self.category, short_description='Lancets',
            price_ugx=55_000, unit='Box of 200', min_order_qty=2,
        )

    def line(self, product, quantity=1, price=1):
        return {'product_id': product.pk, 'sku': product.sku, 'quantity': quantity, 'unit_price_ugx': price}


class CatalogueTests(ShopTestCase):
    def test_lists_only_published_products_without_auth(self):
        Product.objects.create(
            name='Hidden', sku='HID-1', category=self.category, short_description='x', price_ugx=1, unit='x',
            is_active=False,
        )
        res = self.client.get(reverse('product-list'), {'page_size': 500})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()['data']
        self.assertEqual(body['count'], 2)
        first = body['results'][0]
        self.assertEqual(first['category'], 'rapid-tests')
        self.assertIn('price_ugx', first)

    def test_product_detail_by_slug(self):
        res = self.client.get(reverse('product-detail', args=[self.kit.slug]))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()['data']['sku'], 'KIT-25')

    def test_categories(self):
        res = self.client.get(reverse('category-list'))
        self.assertEqual(res.json()['data'], [{'id': 'rapid-tests', 'label': 'Rapid Tests', 'description': ''}])


class OrderTests(ShopTestCase):
    def test_order_is_repriced_from_database_and_emails_both_parties(self):
        payload = order_payload(items=[self.line(self.kit, 2, price=1), self.line(self.lancets, 3, price=1)])
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(reverse('order-create'), payload, format='json')

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        data = res.json()['data']
        self.assertTrue(data['reference'].startswith('ZI-'))
        self.assertEqual(data['subtotal_ugx'], 2 * 275_000 + 3 * 55_000)  # client prices ignored
        self.assertEqual(data['customer_email'], 'amina@clinic.ug')

        order = Order.objects.get(reference=data['reference'])
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.customer.organization, 'Kisoga Health Centre')

        self.assertEqual([m.to for m in mail.outbox], [['amina@clinic.ug'], STAFF_EMAILS])
        self.assertIn(order.reference, mail.outbox[0].subject)
        self.assertIn('HIV Test Kit', mail.outbox[0].alternatives[0][0])
        self.assertTrue(EmailLog.objects.filter(related_object=f'order {order.reference}', sent=True).exists())

    def test_repeat_customer_is_matched_by_email(self):
        for _ in range(2):
            self.client.post(reverse('order-create'), order_payload(items=[self.line(self.kit)]), format='json')
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Customer.objects.get().orders.count(), 2)

    def test_rejects_unavailable_items(self):
        quote_only = Product.objects.create(
            name='Starter Pack', sku='PACK', category=self.category, short_description='x', unit='x', quote_only=True,
        )
        cases = [
            (self.line(quote_only), 'quotation only'),
            (self.line(self.lancets, 1), 'minimum order'),
            ({'product_id': 999, 'quantity': 1}, 'no longer available'),
        ]
        for line, message in cases:
            with self.subTest(message):
                res = self.client.post(reverse('order-create'), order_payload(items=[line]), format='json')
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(message, res.json()['message'])
        self.assertEqual(Order.objects.count(), 0)

    def test_validation_errors_include_field_paths(self):
        payload = order_payload(items=[self.line(self.kit)])
        payload['customer']['email'] = 'not-an-email'
        res = self.client.post(reverse('order-create'), payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer.email', res.json()['field_errors'])


class FormTests(ShopTestCase):
    def test_quote_request_creates_customer_and_notifies(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(reverse('quote-request-create'), {
                'product_id': self.kit.pk, 'quantity': 40, 'full_name': 'Dr Okello', 'email': 'okello@ngo.org',
                'phone': '+256 772 000000', 'organization': 'Health NGO', 'country': 'UG', 'message': '',
            }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        quote = QuoteRequest.objects.get()
        self.assertEqual(quote.sku, 'KIT-25')
        self.assertEqual(quote.customer.email, 'okello@ngo.org')
        self.assertEqual(len(mail.outbox), 2)

    def test_contact_message(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(reverse('contact-create'), {
                'full_name': 'Jane', 'email': 'jane@lab.ug', 'phone': '', 'organization': '', 'topic': 'distribution',
                'subject': 'Distribution', 'message': 'We would like to distribute in Kenya.',
            }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(ContactMessage.objects.get().topic, 'distribution')
        self.assertEqual(len(mail.outbox), 2)

    def test_newsletter_resubscribe_is_idempotent(self):
        for _ in range(2):
            res = self.client.post(reverse('newsletter-subscribe'), {'email': 'News@Lab.ug'}, format='json')
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(NewsletterSubscriber.objects.get().email, 'news@lab.ug')

    @mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'public_forms': '2/min'})
    def test_public_forms_are_throttled(self):
        codes = [
            self.client.post(reverse('newsletter-subscribe'), {'email': f'a{i}@x.ug'}, format='json').status_code
            for i in range(3)
        ]
        self.assertEqual(codes[-1], status.HTTP_429_TOO_MANY_REQUESTS)


class AdminTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        self.admin = CustomUser.objects.create_superuser(email='admin@zakayo.test', password='Str0ng-Passw0rd!')
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('order-create'), order_payload(items=[self.line(self.kit)]), format='json')
        self.order = Order.objects.get()
        mail.outbox.clear()

    def test_admin_pages_load(self):
        for name in ('order', 'product', 'customer', 'quoterequest', 'contactmessage', 'newslettersubscriber', 'emaillog'):
            with self.subTest(name):
                res = self.client.get(reverse(f'admin:shop_{name}_changelist'))
                self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:shop_order_change', args=[self.order.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('admin:shop_product_add')).status_code, 200)

    def test_confirm_action_updates_status_and_emails_customer(self):
        res = self.client.post(reverse('admin:shop_order_changelist'), {
            'action': 'mark_confirmed', '_selected_action': [self.order.pk],
        })
        self.assertEqual(res.status_code, 302)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CONFIRMED)
        self.assertIsNotNone(self.order.confirmed_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('confirmed', mail.outbox[0].subject.lower())

    def test_send_quote_action(self):
        quote = QuoteRequest.objects.create(
            product=self.kit, sku='KIT-25', quantity=10, full_name='Dr A', email='a@ngo.org', phone='+256700000000',
            organization='NGO', country='UG', quoted_unit_price_ugx=250_000,
        )
        self.client.post(reverse('admin:shop_quoterequest_changelist'), {
            'action': 'send_quote', '_selected_action': [quote.pk],
        })
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteRequest.Status.QUOTED)
        self.assertIn('2,500,000', mail.outbox[0].alternatives[0][0])

    def test_compose_email_to_customers(self):
        url = reverse('admin:shop_customer_changelist')
        customer = self.order.customer
        form = self.client.post(url, {'action': 'send_email', '_selected_action': [customer.pk]})
        self.assertContains(form, 'Send email')
        self.client.post(url, {
            'action': 'send_email', '_selected_action': [customer.pk], 'send': '1',
            'subject': 'New stock', 'body': 'New kits are available.',
        })
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'New stock')


class TrackingTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(reverse('order-create'), order_payload(items=[self.line(self.kit)]), format='json')
        self.token = res.json()['data']['tracking_token']
        self.order = Order.objects.get()
        mail.outbox.clear()

    def track_url(self):
        return reverse('order-tracking', args=[self.order.reference])

    def confirm(self, **extra):
        return self.client.post(
            reverse('order-confirm-delivery', args=[self.order.reference]),
            {'token': self.token, 'received_by': 'Nurse Sarah', **extra}, format='json',
        )

    def test_tracking_requires_the_secret_token(self):
        self.assertEqual(self.client.get(self.track_url()).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(self.track_url(), {'token': 'guess'}).status_code, status.HTTP_404_NOT_FOUND)
        res = self.client.get(self.track_url(), {'token': self.token})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()['data']
        self.assertEqual(data['status'], 'pending')
        self.assertFalse(data['can_confirm_delivery'])
        self.assertEqual(data['items'][0]['product_name'], 'HIV Test Kit')
        self.assertNotIn('email', data)  # no contact details beyond what's on the delivery note

    def test_delivery_can_only_be_confirmed_once_dispatched(self):
        self.assertEqual(self.confirm().status_code, status.HTTP_400_BAD_REQUEST)

        self.order.set_status(Order.Status.DISPATCHED)
        self.order.save()
        res = self.confirm(feedback='All good')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()['data']['status'], 'delivered')

        self.order.refresh_from_db()
        self.assertEqual(self.order.received_by, 'Nurse Sarah')
        self.assertIsNotNone(self.order.delivered_at)
        self.assertEqual([m.to for m in mail.outbox], [['amina@clinic.ug'], STAFF_EMAILS])
        self.assertEqual(self.confirm().status_code, status.HTTP_400_BAD_REQUEST)  # not twice

    def test_emails_carry_tracking_link(self):
        self.order.set_status(Order.Status.DISPATCHED)
        self.order.save()
        from shop import emails
        emails.send_order_status_update(self.order)
        self.assertIn(self.order.tracking_token, mail.outbox[0].alternatives[0][0])


class AdminStatusButtonTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(CustomUser.objects.create_superuser(email='ops@zakayo.test', password='Str0ng-Passw0rd!'))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('order-create'), order_payload(items=[self.line(self.kit)]), format='json')
        self.order = Order.objects.get()
        mail.outbox.clear()

    def change_page_form_data(self, **overrides):
        """Minimal POST body matching the order page's form, as the side-panel buttons submit it."""
        data = {
            'status': self.order.status, 'full_name': self.order.full_name, 'organization': self.order.organization,
            'customer_type': self.order.customer_type, 'email': self.order.email, 'phone': self.order.phone,
            'country': self.order.country, 'region': self.order.region, 'city': self.order.city,
            'address': self.order.address, 'notes': '', 'payment_method': self.order.payment_method,
            'delivery_fee_ugx': 0, 'payment_instructions': '', 'internal_notes': '', 'status_note': '',
            'items-TOTAL_FORMS': 0, 'items-INITIAL_FORMS': 0,
        }
        data.update(overrides)
        return data

    def test_button_saves_page_edits_changes_status_and_emails(self):
        url = reverse('admin:shop_order_set_status', args=[self.order.pk, 'confirmed'])
        res = self.client.post(url, self.change_page_form_data(
            delivery_fee_ugx=15_000, payment_instructions='MTN MoMo 0775 720950', status_note='Delivery Friday',
        ))
        self.assertRedirects(res, reverse('admin:shop_order_change', args=[self.order.pk]), fetch_redirect_response=False)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertEqual(self.order.delivery_fee_ugx, 15_000)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn('MTN MoMo 0775 720950', html)
        self.assertIn('Delivery Friday', html)

    def test_staff_cannot_mark_delivered(self):
        self.order.set_status(Order.Status.DISPATCHED)
        self.order.save()
        self.client.post(reverse('admin:shop_order_set_status', args=[self.order.pk, 'delivered']), self.change_page_form_data())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'dispatched')
        page = self.client.get(reverse('admin:shop_order_change', args=[self.order.pk]))
        self.assertContains(page, 'Awaiting the customer')
        self.assertNotContains(page, 'value="delivered"')
        self.assertEqual(len(mail.outbox), 0)

    def test_change_page_shows_buttons_for_allowed_transitions(self):
        page = self.client.get(reverse('admin:shop_order_change', args=[self.order.pk]))
        self.assertContains(page, 'Confirm &amp; email customer')
        self.assertContains(page, 'Cancel order &amp; email')
        self.assertNotContains(page, 'Mark dispatched &amp; email')

    def test_delivery_note_has_qr_code(self):
        res = self.client.get(reverse('admin:shop_order_delivery_note', args=[self.order.pk]))
        self.assertContains(res, '<svg')
        self.assertContains(res, self.order.reference)
        self.assertContains(res, self.order.tracking_token)


class CustomerLookupTests(ShopTestCase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            email='amina@clinic.ug', full_name='Amina Nakato', phone='+256 772 123456',
            organization='Kisoga HC', customer_type='health_facility', region='Mukono', city='Kisoga', address='Main road',
        )

    def lookup(self, phone):
        return self.client.post(reverse('customer-lookup'), {'phone': phone}, format='json')

    def code_from_email(self):
        import re
        return re.search(r'\b(\d{6})\b', mail.outbox[-1].subject).group(1)

    def test_local_and_international_formats_match_and_email_a_code(self):
        res = self.lookup('0772 123 456')
        self.assertEqual(res.json()['data'], {'found': True, 'masked_email': 'am***@clinic.ug'})
        self.assertEqual(mail.outbox[-1].to, ['amina@clinic.ug'])
        self.assertNotIn('Amina', str(res.json()))  # nothing but the masked email before verification

    def test_verify_returns_details_once(self):
        self.lookup('+256772123456')
        code = self.code_from_email()
        res = self.client.post(reverse('customer-verify'), {'phone': '0772123456', 'code': code}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()['data']['organization'], 'Kisoga HC')
        again = self.client.post(reverse('customer-verify'), {'phone': '0772123456', 'code': code}, format='json')
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'customer_lookup': '100/min'})
    def test_wrong_codes_lock_after_max_attempts(self):
        self.lookup('0772123456')
        code = self.code_from_email()
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(CustomerLookupCode.MAX_ATTEMPTS):
            self.client.post(reverse('customer-verify'), {'phone': '0772123456', 'code': wrong}, format='json')
        res = self.client.post(reverse('customer-verify'), {'phone': '0772123456', 'code': code}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_phone(self):
        res = self.lookup('0700 999 999')
        self.assertEqual(res.json()['data'], {'found': False, 'masked_email': ''})
        self.assertEqual(len(mail.outbox), 0)
