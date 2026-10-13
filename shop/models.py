import hashlib
import re
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# Shared choices — values must match the frontend (src/types/index.ts).

COUNTRY_CHOICES = [
    ('UG', 'Uganda'),
    ('KE', 'Kenya'),
    ('TZ', 'Tanzania'),
    ('RW', 'Rwanda'),
    ('BI', 'Burundi'),
    ('SS', 'South Sudan'),
    ('CD', 'DR Congo'),
]

CUSTOMER_TYPE_CHOICES = [
    ('health_facility', 'Hospital / health facility'),
    ('laboratory', 'Laboratory'),
    ('pharmacy', 'Pharmacy / drug shop'),
    ('ngo', 'NGO / programme'),
    ('distributor', 'Distributor'),
    ('individual', 'Individual'),
]

REGULATORY_CHOICES = [
    ('NDA', 'NDA approved'),
    ('ISO 13485', 'ISO 13485'),
    ('ISO 9001', 'ISO 9001'),
    ('CE-IVD', 'CE-IVD'),
    ('WHO PQ', 'WHO PQ'),
]

VISUAL_CHOICES = [
    ('cassette', 'Test cassette'),
    ('self-test', 'Self-test device'),
    ('strip', 'Test strips'),
    ('tube', 'Tubes / containers'),
    ('swab', 'Swabs'),
    ('tips', 'Pipette tips'),
    ('box', 'Boxed product'),
    ('device', 'Device / equipment'),
]


class Category(models.Model):
    slug = models.SlugField(max_length=50, unique=True, help_text='Used in URLs, e.g. "rapid-tests".')
    label = models.CharField(max_length=80)
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ('sort_order', 'label')
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.label


class ProductQuerySet(models.QuerySet):
    def published(self):
        return self.filter(is_active=True)


class Product(TimeStampedModel):
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    sku = models.CharField('SKU', max_length=40, unique=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    short_description = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    price_ugx = models.PositiveIntegerField('price (UGX)', default=0, help_text='Unit price in Uganda Shillings.')
    unit = models.CharField(max_length=60, help_text='What one unit is, e.g. "Kit of 25 tests".')
    min_order_qty = models.PositiveIntegerField('minimum order quantity', default=1)
    regulatory = models.JSONField(default=list, blank=True, help_text='Regulatory marks shown as badges.')
    in_stock = models.BooleanField(default=True)
    quote_only = models.BooleanField(default=False, help_text='Sold by quotation only — hides "Add to cart".')
    featured = models.BooleanField(default=False, help_text='Shown on the home page.')
    is_active = models.BooleanField('published', default=True, help_text='Untick to hide from the website.')
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    visual = models.CharField(
        max_length=20, choices=VISUAL_CHOICES, default='box',
        help_text='Illustration used on the website when no image is uploaded.',
    )
    specs = models.JSONField(default=list, blank=True, help_text='List of {"label": ..., "value": ...} pairs.')
    sort_order = models.PositiveIntegerField(default=0)

    objects = ProductQuerySet.as_manager()

    class Meta:
        ordering = ('sort_order', 'name')

    def __str__(self):
        return f'{self.name} ({self.sku})'

    def clean(self):
        allowed = {value for value, _ in REGULATORY_CHOICES}
        if not isinstance(self.regulatory, list) or not set(self.regulatory) <= allowed:
            raise ValidationError({'regulatory': f'Use any of: {", ".join(sorted(allowed))}.'})
        if not isinstance(self.specs, list) or not all(
            isinstance(s, dict) and set(s) == {'label', 'value'} for s in self.specs
        ):
            raise ValidationError({'specs': 'Use a list of {"label": ..., "value": ...} objects.'})
        if not self.quote_only and self.price_ugx <= 0:
            raise ValidationError({'price_ugx': 'Set a price, or mark the product as quote only.'})

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:170] or 'product'
            slug, n = base, 2
            while Product.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f'{base}-{n}', n + 1
            self.slug = slug
        super().save(*args, **kwargs)


def phone_key(phone):
    """Normalise a phone number for matching: digits only, Ugandan local numbers (07…) as 2567…."""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('0') and len(digits) == 10:
        digits = '256' + digits[1:]
    return digits


class Customer(TimeStampedModel):
    """A buyer, created or updated automatically from orders and quote requests (matched by email)."""

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    organization = models.CharField(max_length=150, blank=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='individual')
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, default='UG')
    region = models.CharField('district / region', max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)
    internal_notes = models.TextField(blank=True, help_text='Visible to staff only.')
    phone_key = models.CharField(max_length=20, blank=True, db_index=True, editable=False)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.full_name} — {self.organization}' if self.organization else self.full_name

    def save(self, *args, **kwargs):
        self.phone_key = phone_key(self.phone)
        if kwargs.get('update_fields') is not None and 'phone' in kwargs['update_fields']:
            kwargs['update_fields'] = {*kwargs['update_fields'], 'phone_key'}
        super().save(*args, **kwargs)

    @classmethod
    def find_by_phone(cls, phone):
        key = phone_key(phone)
        if len(key) < 9:
            return None
        return cls.objects.filter(phone_key=key).order_by('-updated_at').first()

    @property
    def masked_email(self):
        local, _, domain = self.email.partition('@')
        return f'{local[:2]}{"*" * max(3, len(local) - 2)}@{domain}'

    @classmethod
    def upsert(cls, email, **details):
        """Find the customer by email and refresh their contact details with the latest non-empty values."""
        customer, created = cls.objects.get_or_create(email=email.lower(), defaults=details)
        if not created:
            changed = [k for k, v in details.items() if v and getattr(customer, k) != v]
            for key in changed:
                setattr(customer, key, details[key])
            if changed:
                customer.save(update_fields=[*changed, 'updated_at'])
        return customer


class CustomerLookupCode(models.Model):
    """One-time code emailed to a customer so checkout can safely pre-fill their saved details."""

    TTL = timedelta(minutes=10)
    MAX_ATTEMPTS = 5

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='lookup_codes')
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ('-created_at',)

    @staticmethod
    def hash(code):
        return hashlib.sha256(f'{settings.SECRET_KEY}:{code}'.encode()).hexdigest()

    @classmethod
    def issue(cls, customer):
        """Invalidate older codes and return a fresh 6-digit code (the plain code is only ever emailed)."""
        cls.objects.filter(customer=customer, used=False).update(used=True)
        code = f'{secrets.randbelow(1_000_000):06d}'
        cls.objects.create(customer=customer, code_hash=cls.hash(code))
        return code

    @classmethod
    def verify(cls, customer, code):
        entry = cls.objects.filter(
            customer=customer, used=False, created_at__gte=timezone.now() - cls.TTL,
        ).first()
        if entry is None or entry.attempts >= cls.MAX_ATTEMPTS:
            return False
        if secrets.compare_digest(entry.code_hash, cls.hash(code.strip())):
            entry.used = True
            entry.save(update_fields=['used'])
            return True
        entry.attempts += 1
        entry.save(update_fields=['attempts'])
        return False


def generate_order_reference():
    alphabet = string.ascii_uppercase + string.digits
    stamp = timezone.localdate().strftime('%y%m%d')
    while True:
        reference = f"ZI-{stamp}-{''.join(secrets.choice(alphabet) for _ in range(4))}"
        if not Order.objects.filter(reference=reference).exists():
            return reference


def generate_tracking_token():
    return secrets.token_urlsafe(24)


class Order(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        CONFIRMED = 'confirmed', 'Confirmed'
        PROCESSING = 'processing', 'Processing'
        DISPATCHED = 'dispatched', 'Dispatched'
        DELIVERED = 'delivered', 'Delivered'
        CANCELLED = 'cancelled', 'Cancelled'

    PAYMENT_CHOICES = [
        ('mobile_money', 'Mobile Money'),
        ('bank_transfer', 'Bank transfer'),
        ('invoice', 'Invoice / LPO'),
        ('cash_on_delivery', 'Cash on delivery'),
    ]

    # Which statuses staff may move an order to from each status. "Delivered" is never set by staff:
    # the customer confirms receipt by scanning the delivery note's QR code.
    STAFF_TRANSITIONS = {
        Status.PENDING: (Status.CONFIRMED, Status.CANCELLED),
        Status.CONFIRMED: (Status.PROCESSING, Status.DISPATCHED, Status.CANCELLED),
        Status.PROCESSING: (Status.DISPATCHED, Status.CANCELLED),
        Status.DISPATCHED: (),
        Status.DELIVERED: (),
        Status.CANCELLED: (),
    }

    reference = models.CharField(max_length=20, unique=True, default=generate_order_reference, editable=False)
    # Secret part of the customer's tracking link / QR code, so orders can't be looked up by reference alone.
    tracking_token = models.CharField(max_length=64, unique=True, default=generate_tracking_token, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='orders')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)

    # Snapshot of the details as submitted, so later edits to the customer don't rewrite history.
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    organization = models.CharField(max_length=150, blank=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES)
    region = models.CharField('district / region', max_length=100)
    city = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    notes = models.TextField('customer notes', blank=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_CHOICES)
    display_currency = models.CharField(max_length=3, default='UGX', help_text='Currency the customer was browsing in.')

    subtotal_ugx = models.PositiveIntegerField('subtotal (UGX)', default=0)
    delivery_fee_ugx = models.PositiveIntegerField('delivery fee (UGX)', default=0)
    payment_instructions = models.TextField(
        blank=True, help_text='Included in the confirmation email, e.g. Mobile Money number or bank details.',
    )
    internal_notes = models.TextField(blank=True, help_text='Visible to staff only.')

    confirmed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    received_by = models.CharField(max_length=150, blank=True, help_text='Name given by the customer on delivery.')
    delivery_feedback = models.TextField(blank=True, help_text='Comment left by the customer on delivery.')

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.reference

    @property
    def tracking_url(self):
        return f'{settings.SITE_URL}/track/{self.reference}?t={self.tracking_token}'

    @property
    def can_confirm_delivery(self):
        return self.status == self.Status.DISPATCHED

    def allowed_staff_transitions(self):
        return self.STAFF_TRANSITIONS.get(self.status, ())

    def confirm_delivery(self, received_by='', feedback=''):
        if not self.can_confirm_delivery:
            raise ValidationError('Only dispatched orders can be confirmed as delivered.')
        self.set_status(self.Status.DELIVERED)
        self.received_by, self.delivery_feedback = received_by, feedback
        self.save(update_fields=['status', 'delivered_at', 'received_by', 'delivery_feedback', 'updated_at'])

    @property
    def total_ugx(self):
        return self.subtotal_ugx + self.delivery_fee_ugx

    def recalculate_subtotal(self):
        self.subtotal_ugx = sum(item.line_total_ugx for item in self.items.all())

    def set_status(self, status):
        """Change status and stamp the matching milestone date."""
        self.status = status
        now = timezone.now()
        stamp = {
            self.Status.CONFIRMED: 'confirmed_at',
            self.Status.DISPATCHED: 'dispatched_at',
            self.Status.DELIVERED: 'delivered_at',
        }.get(status)
        if stamp and not getattr(self, stamp):
            setattr(self, stamp, now)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, related_name='order_items')
    product_name = models.CharField(max_length=160)
    sku = models.CharField('SKU', max_length=40)
    unit = models.CharField(max_length=60)
    unit_price_ugx = models.PositiveIntegerField('unit price (UGX)')
    quantity = models.PositiveIntegerField()

    class Meta:
        ordering = ('id',)

    def __str__(self):
        return f'{self.quantity} × {self.product_name}'

    @property
    def line_total_ugx(self):
        return self.unit_price_ugx * self.quantity


class QuoteRequest(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = 'new', 'New'
        QUOTED = 'quoted', 'Quote sent'
        WON = 'won', 'Won'
        LOST = 'lost', 'Lost'
        CLOSED = 'closed', 'Closed'

    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='quote_requests')
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, related_name='quote_requests')
    sku = models.CharField('SKU', max_length=40)
    quantity = models.PositiveIntegerField()
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    organization = models.CharField(max_length=150)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES)
    message = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)

    quoted_unit_price_ugx = models.PositiveIntegerField('quoted unit price (UGX)', null=True, blank=True)
    quote_valid_until = models.DateField(null=True, blank=True)
    quote_message = models.TextField(blank=True, help_text='Included in the quote email to the customer.')
    quoted_at = models.DateTimeField(null=True, blank=True)
    internal_notes = models.TextField(blank=True, help_text='Visible to staff only.')

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'Quote #{self.pk} — {self.full_name}'

    @property
    def product_name(self):
        return self.product.name if self.product else self.sku

    @property
    def quoted_total_ugx(self):
        return self.quoted_unit_price_ugx * self.quantity if self.quoted_unit_price_ugx else None


class ContactMessage(TimeStampedModel):
    class Status(models.TextChoices):
        NEW = 'new', 'New'
        REPLIED = 'replied', 'Replied'
        CLOSED = 'closed', 'Closed'

    TOPIC_CHOICES = [
        ('sales', 'Sales & orders'),
        ('distribution', 'Distribution inquiry'),
        ('partnership', 'Partnership'),
        ('quality', 'Quality & regulatory'),
        ('general', 'General'),
    ]

    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    organization = models.CharField(max_length=150, blank=True)
    topic = models.CharField(max_length=20, choices=TOPIC_CHOICES, default='general')
    subject = models.CharField(max_length=200)
    message = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    reply = models.TextField(blank=True, help_text='Write a reply, then use “Send reply” to email it.')
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.subject} — {self.full_name}'


class NewsletterSubscriber(TimeStampedModel):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField('subscribed', default=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return self.email


class EmailLog(models.Model):
    """Every email the shop sends, for support and auditing."""

    created_at = models.DateTimeField(auto_now_add=True)
    to = models.CharField(max_length=500)
    subject = models.CharField(max_length=255)
    template = models.CharField(max_length=100)
    related_object = models.CharField(max_length=100, blank=True, help_text='e.g. order ZI-251013-AB12')
    sent = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.subject} → {self.to}'
