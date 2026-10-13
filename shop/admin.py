import segno
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.contrib.humanize.templatetags.humanize import intcomma
from django.db.models import Count, Sum
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from import_export import fields, resources, widgets
from import_export.admin import ExportMixin, ImportExportModelAdmin

from . import emails
from .models import (
    REGULATORY_CHOICES, Category, ContactMessage, Customer, EmailLog, NewsletterSubscriber, Order, OrderItem, Product,
    QuoteRequest,
)

STATUS_COLOURS = {
    'pending': '#b45309', 'new': '#b45309',
    'confirmed': '#043a80', 'processing': '#6d28d9', 'quoted': '#043a80', 'replied': '#047857',
    'dispatched': '#0e7490', 'delivered': '#047857', 'won': '#047857',
    'cancelled': '#b91c1c', 'lost': '#b91c1c', 'closed': '#6b7280',
}


def status_badge(value, label):
    colour = STATUS_COLOURS.get(value, '#6b7280')
    return format_html(
        '<span style="display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;'
        'color:#fff;background:{}">{}</span>',
        colour, label,
    )


def ugx(amount):
    return f'UGX {intcomma(amount)}' if amount is not None else '—'


# --- Bulk email (intermediate "compose" page) -------------------------------

class ComposeEmailForm(forms.Form):
    subject = forms.CharField(max_length=200)
    body = forms.CharField(widget=forms.Textarea(attrs={'rows': 10, 'style': 'width:100%'}), label='Message')


def compose_email_action(modeladmin, request, queryset, recipients):
    """
    Shared admin action: show a compose form, then email each recipient individually.
    `recipients` is a list of (email, name, related_object) tuples.
    """
    if 'send' in request.POST:
        form = ComposeEmailForm(request.POST)
        if form.is_valid():
            sent = sum(
                emails.send_custom_message(email, name, form.cleaned_data['subject'], form.cleaned_data['body'], ref)
                for email, name, ref in recipients
            )
            failed = len(recipients) - sent
            modeladmin.message_user(request, f'Email sent to {sent} recipient(s).', messages.SUCCESS)
            if failed:
                modeladmin.message_user(request, f'{failed} email(s) failed — see Email log.', messages.ERROR)
            return None
    else:
        form = ComposeEmailForm()

    return TemplateResponse(request, 'admin/shop/compose_email.html', {
        **modeladmin.admin_site.each_context(request),
        'title': 'Send email',
        'form': form,
        'recipients': recipients,
        'queryset': queryset,
        'action': request.POST.get('action'),
        'opts': modeladmin.model._meta,
        'action_checkbox_name': admin.helpers.ACTION_CHECKBOX_NAME,
    })


# --- Catalogue --------------------------------------------------------------

class ProductAdminForm(forms.ModelForm):
    regulatory = forms.MultipleChoiceField(
        choices=REGULATORY_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        help_text='Regulatory marks shown as badges on the website.',
    )

    class Meta:
        model = Product
        fields = '__all__'
        widgets = {
            'specs': forms.Textarea(attrs={
                'rows': 6, 'style': 'font-family:monospace;width:100%',
                'placeholder': '[{"label": "Specimen", "value": "Whole blood"}]',
            }),
            'short_description': forms.TextInput(attrs={'style': 'width:100%'}),
        }


class ProductResource(resources.ModelResource):
    """CSV/XLSX import & export. Rows are matched on SKU; category is given by its slug."""

    category = fields.Field(
        column_name='category', attribute='category', widget=widgets.ForeignKeyWidget(Category, field='slug'),
    )

    class Meta:
        model = Product
        import_id_fields = ('sku',)
        skip_unchanged = True
        fields = (
            'sku', 'name', 'slug', 'category', 'short_description', 'description', 'price_ugx', 'unit',
            'min_order_qty', 'regulatory', 'in_stock', 'quote_only', 'featured', 'is_active', 'visual', 'specs',
            'sort_order',
        )
        export_order = fields


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('label', 'slug', 'product_count', 'sort_order')
    list_editable = ('sort_order',)
    prepopulated_fields = {'slug': ('label',)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_products=Count('products'))

    @admin.display(description='Products', ordering='_products')
    def product_count(self, obj):
        return obj._products


@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin):
    form = ProductAdminForm
    resource_classes = [ProductResource]
    list_display = ('thumbnail', 'name', 'sku', 'category', 'price_display', 'in_stock', 'featured', 'is_active')
    list_display_links = ('thumbnail', 'name')
    list_editable = ('in_stock', 'featured', 'is_active')
    list_filter = ('category', 'is_active', 'in_stock', 'featured', 'quote_only')
    search_fields = ('name', 'sku', 'short_description')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('image_preview', 'created_at', 'updated_at')
    list_per_page = 50
    actions = ('publish', 'unpublish', 'mark_in_stock', 'mark_out_of_stock')
    fieldsets = (
        (None, {'fields': ('name', 'slug', 'sku', 'category', 'short_description', 'description')}),
        ('Pricing & availability', {'fields': ('price_ugx', 'unit', 'min_order_qty', 'quote_only', 'in_stock')}),
        ('Display', {'fields': ('is_active', 'featured', 'sort_order', 'image', 'image_preview', 'visual')}),
        ('Compliance & specifications', {'fields': ('regulatory', 'specs')}),
        ('History', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    @admin.display(description='')
    def thumbnail(self, obj):
        if not obj.image:
            return mark_safe('<span style="color:#9ca3af">—</span>')
        return format_html('<img src="{}" style="height:40px;width:40px;object-fit:cover;border-radius:6px">', obj.image.url)

    @admin.display(description='Preview')
    def image_preview(self, obj):
        if not obj.image:
            return 'No image — the website shows the selected illustration instead.'
        return format_html('<img src="{}" style="max-height:220px;border-radius:10px">', obj.image.url)

    @admin.display(description='Price', ordering='price_ugx')
    def price_display(self, obj):
        return 'On request' if obj.quote_only else ugx(obj.price_ugx)

    @admin.action(description='Publish on website')
    def publish(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_active=True)} product(s) published.')

    @admin.action(description='Hide from website')
    def unpublish(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_active=False)} product(s) hidden.')

    @admin.action(description='Mark in stock')
    def mark_in_stock(self, request, queryset):
        self.message_user(request, f'{queryset.update(in_stock=True)} product(s) marked in stock.')

    @admin.action(description='Mark out of stock')
    def mark_out_of_stock(self, request, queryset):
        self.message_user(request, f'{queryset.update(in_stock=False)} product(s) marked out of stock.')


# --- Orders -----------------------------------------------------------------

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = ('product_name', 'sku', 'unit', 'quantity', 'unit_price_display', 'line_total_display')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Unit price')
    def unit_price_display(self, obj):
        return ugx(obj.unit_price_ugx)

    @admin.display(description='Line total')
    def line_total_display(self, obj):
        return ugx(obj.line_total_ugx)


class OrderAdminForm(forms.ModelForm):
    notify_customer = forms.BooleanField(
        required=False, initial=True, label='Email the customer about a status change',
        help_text='When you change the status and save, the customer gets the matching update email.',
    )
    status_note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 3}), label='Message for the customer (optional)',
        help_text='Added to the status email, e.g. delivery date or tracking details.',
    )

    class Meta:
        model = Order
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'status' in self.fields and self.instance.pk:
            allowed = {self.instance.status, *self.instance.allowed_staff_transitions()}
            self.fields['status'].choices = [c for c in Order.Status.choices if c[0] in allowed]
            if self.instance.status == Order.Status.DISPATCHED:
                self.fields['status'].help_text = 'Delivered is set by the customer when they scan the delivery note QR code.'


class OrderResource(resources.ModelResource):
    class Meta:
        model = Order
        fields = (
            'reference', 'created_at', 'status', 'full_name', 'organization', 'email', 'phone', 'customer_type',
            'country', 'region', 'city', 'address', 'payment_method', 'subtotal_ugx', 'delivery_fee_ugx',
            'confirmed_at', 'dispatched_at', 'delivered_at',
        )
        export_order = fields


def _status_action(new_status, description):
    def action(modeladmin, request, queryset):
        updated, notified = 0, 0
        skipped = 0
        for order in queryset.select_related('customer').prefetch_related('items'):
            if new_status not in order.allowed_staff_transitions():
                skipped += 1
                continue
            order.set_status(new_status)
            order.save()
            updated += 1
            notified += emails.send_order_status_update(order)
        modeladmin.message_user(
            request, f'{updated} order(s) marked {new_status}; {notified} customer email(s) sent.', messages.SUCCESS,
        )
        if skipped:
            modeladmin.message_user(
                request, f'{skipped} order(s) skipped — they can’t move to “{new_status}” from their current status.',
                messages.WARNING,
            )

    action.__name__ = f'mark_{new_status}'
    return admin.action(description=description)(action)


@admin.register(Order)
class OrderAdmin(ExportMixin, admin.ModelAdmin):
    form = OrderAdminForm
    change_form_template = 'admin/shop/order/change_form.html'
    resource_classes = [OrderResource]
    inlines = (OrderItemInline,)
    list_display = (
        'reference', 'created_at', 'customer_display', 'country', 'item_count', 'total_display', 'status_display',
        'payment_method', 'print_link',
    )
    list_filter = ('status', 'payment_method', 'country', 'customer_type', 'created_at')
    search_fields = ('reference', 'full_name', 'organization', 'email', 'phone')
    date_hierarchy = 'created_at'
    list_per_page = 50
    readonly_fields = (
        'reference', 'customer_link', 'subtotal_display', 'total_display', 'display_currency',
        'created_at', 'updated_at', 'confirmed_at', 'dispatched_at', 'delivered_at', 'email_history',
        'received_by', 'delivery_feedback', 'tracking_link',
    )
    fieldsets = (
        ('Order', {'fields': ('reference', 'status', 'notify_customer', 'status_note', 'created_at')}),
        ('Customer', {'fields': (
            'customer_link', 'full_name', 'organization', 'customer_type', 'email', 'phone',
        )}),
        ('Delivery', {'fields': ('country', 'region', 'city', 'address', 'notes')}),
        ('Payment', {'fields': (
            'payment_method', 'subtotal_display', 'delivery_fee_ugx', 'total_display', 'payment_instructions',
            'display_currency',
        )}),
        ('Delivery confirmation', {'fields': ('tracking_link', 'received_by', 'delivery_feedback', 'delivered_at')}),
        ('Staff', {'fields': ('internal_notes', 'email_history')}),
        ('Timeline', {'fields': ('confirmed_at', 'dispatched_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    actions = (
        _status_action(Order.Status.CONFIRMED, 'Confirm & email customer'),
        _status_action(Order.Status.PROCESSING, 'Mark processing & email customer'),
        _status_action(Order.Status.DISPATCHED, 'Mark dispatched & email customer'),
        _status_action(Order.Status.CANCELLED, 'Cancel & email customer'),
        'resend_confirmation',
    )

    STATUS_BUTTONS = {
        Order.Status.CONFIRMED: ('Confirm & email customer', 'btn-primary'),
        Order.Status.PROCESSING: ('Mark processing & email', 'btn-info'),
        Order.Status.DISPATCHED: ('Mark dispatched & email', 'btn-success'),
        Order.Status.CANCELLED: ('Cancel order & email', 'btn-outline-danger'),
    }

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_items=Count('items'))

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path('<path:object_id>/set-status/<str:status>/', view(self.set_status_view), name='shop_order_set_status'),
            path('<path:object_id>/delivery-note/', view(self.delivery_note_view), name='shop_order_delivery_note'),
            *super().get_urls(),
        ]

    def change_view(self, request, object_id, form_url='', extra_context=None):
        order = self.get_object(request, object_id)
        if order is not None:
            extra_context = {
                **(extra_context or {}),
                'status_buttons': [
                    (reverse('admin:shop_order_set_status', args=[order.pk, status]), *self.STATUS_BUTTONS[status])
                    for status in order.allowed_staff_transitions()
                ],
                'delivery_note_url': reverse('admin:shop_order_delivery_note', args=[order.pk]),
            }
        return super().change_view(request, object_id, form_url, extra_context)

    def set_status_view(self, request, object_id, status):
        """
        Side-panel status buttons. They submit the order page's own form here, so any edits on the page
        (delivery fee, payment instructions, message for the customer) are saved together with the new status.
        """
        if request.method != 'POST':
            return redirect('admin:shop_order_change', object_id)
        order = get_object_or_404(Order, pk=object_id)
        if not self.has_change_permission(request, order):
            raise PermissionDenied
        change_url = reverse('admin:shop_order_change', args=[order.pk])
        if status not in order.allowed_staff_transitions():
            self.message_user(request, f'An order that is “{order.get_status_display()}” can’t move to “{status}”.', messages.ERROR)
            return redirect(change_url)

        form = self.get_form(request, order, change=True)(request.POST, request.FILES, instance=order)
        if not form.is_valid():
            self.message_user(request, 'Fix the errors on the page, save, then try again.', messages.ERROR)
            return redirect(change_url)
        order = form.save(commit=False)
        order.set_status(status)
        order.save()
        self.log_change(request, order, f'Status changed to {order.get_status_display()} (button).')

        if emails.send_order_status_update(order, note=form.cleaned_data.get('status_note', '')):
            self.message_user(request, f'Order {order.reference} is now “{order.get_status_display()}”. Customer emailed.', messages.SUCCESS)
        else:
            self.message_user(request, 'Status updated, but the email failed — see Email log.', messages.WARNING)
        if status == Order.Status.DISPATCHED:
            self.message_user(request, 'Print the delivery note so the customer can scan the QR code on arrival.', messages.INFO)
        return redirect(change_url)

    def delivery_note_view(self, request, object_id):
        order = get_object_or_404(Order.objects.prefetch_related('items'), pk=object_id)
        if not self.has_view_permission(request, order):
            raise PermissionDenied
        qr = segno.make(order.tracking_url, error='m')
        return TemplateResponse(request, 'admin/shop/order/delivery_note.html', {
            'order': order,
            'items': order.items.all(),
            'qr_svg': qr.svg_inline(scale=5, border=0, dark='#043a80'),
            'tracking_url': order.tracking_url,
            'printed_at': timezone.localtime(),
        })

    def has_add_permission(self, request):
        return False  # Orders come from the website checkout.

    def save_model(self, request, obj, form, change):
        status_changed = change and 'status' in form.changed_data
        if status_changed:
            obj.set_status(obj.status)
        super().save_model(request, obj, form, change)
        if status_changed and form.cleaned_data.get('notify_customer'):
            if emails.send_order_status_update(obj, note=form.cleaned_data.get('status_note', '')):
                self.message_user(request, f'Status email sent to {obj.email}.', messages.SUCCESS)
            else:
                self.message_user(request, 'Status saved, but the email failed — see Email log.', messages.ERROR)

    @admin.action(description='Re-send order confirmation email')
    def resend_confirmation(self, request, queryset):
        sent = sum(
            emails.send_templated_email(
                'order_confirmation', o.email, f'We’ve received your order {o.reference}',
                {'order': o, 'items': list(o.items.all())}, related_object=f'order {o.reference}',
            )
            for o in queryset.prefetch_related('items')
        )
        self.message_user(request, f'{sent} confirmation email(s) sent.')

    @admin.display(description='')
    def print_link(self, obj):
        url = reverse('admin:shop_order_delivery_note', args=[obj.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener" title="Print delivery note">🖨 Print</a>', url)

    @admin.display(description='Customer tracking link')
    def tracking_link(self, obj):
        return format_html(
            '<a href="{0}" target="_blank" rel="noopener">{0}</a><br><small class="text-muted">'
            'Printed as a QR code on the delivery note. The customer confirms delivery there.</small>',
            obj.tracking_url,
        )

    @admin.display(description='Customer', ordering='full_name')
    def customer_display(self, obj):
        return f'{obj.full_name} ({obj.organization})' if obj.organization else obj.full_name

    @admin.display(description='Customer record')
    def customer_link(self, obj):
        url = reverse('admin:shop_customer_change', args=[obj.customer_id])
        return format_html('<a href="{}">{}</a>', url, obj.customer)

    @admin.display(description='Items', ordering='_items')
    def item_count(self, obj):
        return obj._items

    @admin.display(description='Subtotal')
    def subtotal_display(self, obj):
        return ugx(obj.subtotal_ugx)

    @admin.display(description='Total')
    def total_display(self, obj):
        return ugx(obj.total_ugx)

    @admin.display(description='Status', ordering='status')
    def status_display(self, obj):
        return status_badge(obj.status, obj.get_status_display())

    @admin.display(description='Emails sent')
    def email_history(self, obj):
        logs = EmailLog.objects.filter(related_object=f'order {obj.reference}')[:20]
        if not logs:
            return '—'
        items = format_html_join(
            '', '<li>{} · {} · {}</li>',
            (
                (timezone.localtime(log.created_at).strftime('%d %b %Y %H:%M'), log.subject, 'sent' if log.sent else 'FAILED')
                for log in logs
            ),
        )
        return format_html('<ul style="margin:0;padding-left:16px">{}</ul>', items)


# --- Customers --------------------------------------------------------------

class CustomerOrderInline(admin.TabularInline):
    model = Order
    extra = 0
    can_delete = False
    fields = ('order_link', 'created_at', 'status', 'total')
    readonly_fields = fields
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Order')
    def order_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse('admin:shop_order_change', args=[obj.pk]), obj.reference)

    @admin.display(description='Total')
    def total(self, obj):
        return ugx(obj.total_ugx)


@admin.register(Customer)
class CustomerAdmin(ImportExportModelAdmin):
    inlines = (CustomerOrderInline,)
    list_display = ('full_name', 'organization', 'customer_type', 'email', 'phone', 'country', 'order_count', 'lifetime_value', 'created_at')
    list_filter = ('customer_type', 'country', 'created_at')
    search_fields = ('full_name', 'organization', 'email', 'phone')
    readonly_fields = ('created_at', 'updated_at')
    actions = ('send_email',)
    fieldsets = (
        (None, {'fields': ('full_name', 'organization', 'customer_type', 'email', 'phone')}),
        ('Address', {'fields': ('country', 'region', 'city', 'address')}),
        ('Staff', {'fields': ('internal_notes', 'created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _orders=Count('orders', distinct=True),
            _value=Sum('orders__subtotal_ugx'),
        )

    @admin.display(description='Orders', ordering='_orders')
    def order_count(self, obj):
        return obj._orders

    @admin.display(description='Order value', ordering='_value')
    def lifetime_value(self, obj):
        return ugx(obj._value or 0)

    @admin.action(description='Send email to selected customers')
    def send_email(self, request, queryset):
        recipients = [(c.email, c.full_name, f'customer #{c.pk}') for c in queryset]
        return compose_email_action(self, request, queryset, recipients)


# --- Quotes & messages ------------------------------------------------------

@admin.register(QuoteRequest)
class QuoteRequestAdmin(ExportMixin, admin.ModelAdmin):
    list_display = ('id', 'created_at', 'full_name', 'organization', 'product', 'quantity', 'quoted_total', 'status_display')
    list_filter = ('status', 'country', 'created_at')
    search_fields = ('full_name', 'organization', 'email', 'sku', 'product__name')
    date_hierarchy = 'created_at'
    readonly_fields = ('customer', 'created_at', 'updated_at', 'quoted_at')
    autocomplete_fields = ('product',)
    actions = ('send_quote', 'mark_won', 'mark_lost')
    fieldsets = (
        ('Request', {'fields': ('status', 'product', 'sku', 'quantity', 'message', 'created_at')}),
        ('Contact', {'fields': ('customer', 'full_name', 'organization', 'email', 'phone', 'country')}),
        ('Quotation', {
            'fields': ('quoted_unit_price_ugx', 'quote_valid_until', 'quote_message', 'quoted_at'),
            'description': 'Fill in the price, save, then use the “Send quote to customer” action.',
        }),
        ('Staff', {'fields': ('internal_notes', 'updated_at')}),
    )

    @admin.display(description='Status', ordering='status')
    def status_display(self, obj):
        return status_badge(obj.status, obj.get_status_display())

    @admin.display(description='Quoted total')
    def quoted_total(self, obj):
        return ugx(obj.quoted_total_ugx)

    @admin.action(description='Send quote to customer')
    def send_quote(self, request, queryset):
        sent, skipped = 0, 0
        for quote in queryset.select_related('product'):
            if not quote.quoted_unit_price_ugx:
                skipped += 1
                continue
            if emails.send_quote(quote):
                quote.status, quote.quoted_at = QuoteRequest.Status.QUOTED, timezone.now()
                quote.save(update_fields=['status', 'quoted_at', 'updated_at'])
                sent += 1
        self.message_user(request, f'{sent} quote(s) sent.', messages.SUCCESS if sent else messages.WARNING)
        if skipped:
            self.message_user(request, f'{skipped} skipped — set a quoted unit price first.', messages.WARNING)

    @admin.action(description='Mark won')
    def mark_won(self, request, queryset):
        queryset.update(status=QuoteRequest.Status.WON)

    @admin.action(description='Mark lost')
    def mark_lost(self, request, queryset):
        queryset.update(status=QuoteRequest.Status.LOST)


@admin.register(ContactMessage)
class ContactMessageAdmin(ExportMixin, admin.ModelAdmin):
    list_display = ('created_at', 'full_name', 'organization', 'topic', 'subject', 'status_display')
    list_filter = ('status', 'topic', 'created_at')
    search_fields = ('full_name', 'organization', 'email', 'subject', 'message')
    date_hierarchy = 'created_at'
    readonly_fields = ('full_name', 'email', 'phone', 'organization', 'topic', 'subject', 'message', 'created_at', 'replied_at')
    actions = ('send_reply', 'mark_closed')
    fieldsets = (
        ('Message', {'fields': ('status', 'topic', 'subject', 'message', 'created_at')}),
        ('From', {'fields': ('full_name', 'organization', 'email', 'phone')}),
        ('Reply', {
            'fields': ('reply', 'replied_at'),
            'description': 'Write your reply, save, then use the “Send reply” action.',
        }),
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description='Status', ordering='status')
    def status_display(self, obj):
        return status_badge(obj.status, obj.get_status_display())

    @admin.action(description='Send reply')
    def send_reply(self, request, queryset):
        sent, skipped = 0, 0
        for message in queryset:
            if not message.reply.strip():
                skipped += 1
                continue
            if emails.send_contact_reply(message):
                message.status, message.replied_at = ContactMessage.Status.REPLIED, timezone.now()
                message.save(update_fields=['status', 'replied_at', 'updated_at'])
                sent += 1
        self.message_user(request, f'{sent} repl(ies) sent.', messages.SUCCESS if sent else messages.WARNING)
        if skipped:
            self.message_user(request, f'{skipped} skipped — write a reply first.', messages.WARNING)

    @admin.action(description='Mark closed')
    def mark_closed(self, request, queryset):
        queryset.update(status=ContactMessage.Status.CLOSED)


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(ExportMixin, admin.ModelAdmin):
    list_display = ('email', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('email',)
    actions = ('send_email', 'unsubscribe')

    @admin.action(description='Send email to selected subscribers')
    def send_email(self, request, queryset):
        recipients = [(s.email, '', f'newsletter #{s.pk}') for s in queryset.filter(is_active=True)]
        return compose_email_action(self, request, queryset, recipients)

    @admin.action(description='Unsubscribe')
    def unsubscribe(self, request, queryset):
        queryset.update(is_active=False)


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'to', 'subject', 'template', 'related_object', 'sent')
    list_filter = ('sent', 'template', 'created_at')
    search_fields = ('to', 'subject', 'related_object')
    readonly_fields = [f.name for f in EmailLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
