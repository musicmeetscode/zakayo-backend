import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import EmailLog, StockAlert

logger = logging.getLogger(__name__)


def _base_context():
    return {
        'site_url': settings.SITE_URL,
        'logo_url': settings.EMAIL_LOGO_URL,
    }


def send_templated_email(template, to, subject, context=None, related_object='', reply_to=None):
    """
    Render emails/<template>.html (+ .txt when present, else a stripped-HTML fallback), store it
    in EmailLog and hand it to the background worker (see deliver()).
    Never raises: failures are recorded on the EmailLog so a broken mail server can't break checkout.
    Returns True when the message was queued (or sent, when EMAIL_IN_BACKGROUND is off).
    """
    recipients = [to] if isinstance(to, str) else [r for r in to if r]
    if not recipients:
        return False

    ctx = {**_base_context(), **(context or {})}
    reply_to = reply_to or settings.SHOP_REPLY_TO or []
    log = EmailLog(
        to=', '.join(recipients), reply_to=', '.join(reply_to), subject=subject, template=template,
        related_object=related_object,
    )
    try:
        log.html_body = render_to_string(f'emails/{template}.html', ctx)
        try:
            log.text_body = render_to_string(f'emails/{template}.txt', ctx)
        except TemplateDoesNotExist:
            log.text_body = strip_tags(log.html_body)
    except Exception as exc:  # noqa: BLE001 — a template bug must not break the request
        logger.exception('Failed to render %s email', template)
        log.status, log.error = EmailLog.Status.FAILED, str(exc)
        log.save()
        return False

    log.save()
    return queue_delivery(log)


def queue_delivery(log):
    """Send in the background once the surrounding transaction commits (or inline, if configured)."""
    if log.status != EmailLog.Status.QUEUED:
        EmailLog.objects.filter(pk=log.pk).update(status=EmailLog.Status.QUEUED, error='')
        log.status = EmailLog.Status.QUEUED
    if not settings.EMAIL_IN_BACKGROUND:
        return deliver(log)
    transaction.on_commit(lambda: _enqueue(log))
    return True


def _enqueue(log):
    from .tasks import deliver_email

    try:
        # retry=False: fail fast if the broker is down instead of blocking the request.
        deliver_email.apply_async((log.pk,), retry=False)
    except Exception:  # noqa: BLE001 — broker unreachable: don't lose the email, send it now
        logger.warning('Email queue unavailable; sending email #%s inline', log.pk, exc_info=True)
        deliver(log)


def deliver(log):
    """Send a stored EmailLog through the mail backend. Records the outcome; returns True on success."""
    log.attempts += 1
    try:
        message = EmailMultiAlternatives(
            subject=log.subject,
            body=log.text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=log.recipients,
            reply_to=log.reply_to_list or None,
        )
        message.attach_alternative(log.html_body, 'text/html')
        message.send()
    except Exception as exc:  # noqa: BLE001 — any mail failure must be recorded, not raised
        logger.exception('Failed to send email #%s (%s) to %s', log.pk, log.template, log.to)
        log.status, log.error = EmailLog.Status.FAILED, str(exc)
        log.save(update_fields=['attempts', 'status', 'error'])
        return False
    log.status, log.error, log.sent_at = EmailLog.Status.SENT, '', timezone.now()
    log.save(update_fields=['attempts', 'status', 'error', 'sent_at'])
    return True


# --- Order emails -----------------------------------------------------------

def _order_context(order):
    return {
        'order': order,
        'items': list(order.items.all()),
        'tracking_url': order.tracking_url,
    }


def send_order_received(order):
    """Customer confirmation + staff alert for a new web order."""
    ctx = _order_context(order)
    send_templated_email(
        'order_confirmation', order.email, f'We’ve received your order {order.reference}', ctx,
        related_object=f'order {order.reference}',
    )
    if settings.SALES_NOTIFICATION_EMAILS:
        send_templated_email(
            'order_notification_staff', settings.SALES_NOTIFICATION_EMAILS,
            f'New order {order.reference} — {order.full_name}',
            {**ctx, 'admin_url': f'{settings.BACKEND_URL}/{settings.ADMIN_URL}shop/order/{order.pk}/change/'},
            related_object=f'order {order.reference}',
            reply_to=[order.email],
        )


STATUS_COPY = {
    'confirmed': ('Your order is confirmed', 'Good news — we’ve confirmed stock for your order. Details are below.'),
    'processing': ('Your order is being prepared', 'Our team is now packing your order.'),
    'dispatched': ('Your order is on its way', 'Your order has left our warehouse and is on its way to you.'),
    'delivered': ('Thank you — delivery confirmed', 'Thanks for confirming you’ve received your order. We appreciate your business.'),
    'cancelled': ('Your order has been cancelled', 'Your order has been cancelled. If this is unexpected, please get in touch.'),
}


def send_order_status_update(order, note=''):
    """Email the customer about the order's current status. Returns True if sent."""
    headline, intro = STATUS_COPY.get(order.status, ('Order update', 'There’s an update on your order.'))
    return send_templated_email(
        'order_status', order.email, f'{headline} — {order.reference}',
        {**_order_context(order), 'headline': headline, 'intro': intro, 'note': note},
        related_object=f'order {order.reference}',
    )


def send_delivery_confirmed(order):
    """Customer thank-you + staff alert once the customer confirms receipt."""
    send_order_status_update(order)
    notify_staff(
        f'Delivered: {order.reference} — {order.full_name}',
        f'Order {order.reference} confirmed delivered',
        [
            ('Customer', f'{order.full_name} ({order.organization})' if order.organization else order.full_name),
            ('Received by', order.received_by or '—'),
            ('Feedback', order.delivery_feedback or '—'),
            ('Delivered at', order.delivered_at.strftime('%d %b %Y %H:%M') if order.delivered_at else '—'),
        ],
        f'shop/order/{order.pk}/change/',
    )


def send_back_in_stock(product):
    """Email everyone waiting for this product (one email each), then mark their alerts as done."""
    product.refresh_from_db()
    if not (product.in_stock and product.is_active and not product.quote_only):
        return 0
    alerts = list(product.stock_alerts.filter(notified_at__isnull=True))
    for alert in alerts:
        send_templated_email(
            'back_in_stock', alert.email, f'Back in stock: {product.name}',
            {'product': product, 'product_url': f'{settings.SITE_URL}/shop/{product.slug}'},
            related_object=f'stock alert #{alert.pk}',
        )
    StockAlert.objects.filter(pk__in=[a.pk for a in alerts]).update(notified_at=timezone.now())
    return len(alerts)


# --- Quotes, contact, ad-hoc ------------------------------------------------

def notify_staff(subject, heading, rows, admin_path, reply_to=None):
    if not settings.SALES_NOTIFICATION_EMAILS:
        return
    send_templated_email(
        'staff_notification', settings.SALES_NOTIFICATION_EMAILS, subject,
        {'heading': heading, 'rows': rows, 'admin_url': f'{settings.BACKEND_URL}/{settings.ADMIN_URL}{admin_path}'},
        reply_to=reply_to,
    )


def send_quote_received(quote):
    send_templated_email(
        'quote_request_ack', quote.email, 'We’ve received your quote request', {'quote': quote},
        related_object=f'quote #{quote.pk}',
    )
    notify_staff(
        f'New quote request — {quote.organization}',
        f'Quote request #{quote.pk}',
        [
            ('Product', f'{quote.product_name} ({quote.sku})'),
            ('Quantity', quote.quantity),
            ('Name', quote.full_name),
            ('Organisation', quote.organization),
            ('Email', quote.email),
            ('Phone', quote.phone),
            ('Country', quote.get_country_display()),
            ('Notes', quote.message or '—'),
        ],
        f'shop/quoterequest/{quote.pk}/change/',
        reply_to=[quote.email],
    )


def send_quote(quote):
    return send_templated_email(
        'quote_response', quote.email, f'Your quotation — {quote.product_name}', {'quote': quote},
        related_object=f'quote #{quote.pk}',
    )


def send_contact_received(message):
    send_templated_email(
        'contact_ack', message.email, 'We’ve received your message', {'message': message},
        related_object=f'contact #{message.pk}',
    )
    notify_staff(
        f'[{message.get_topic_display()}] {message.subject}',
        f'New message from {message.full_name}',
        [
            ('Topic', message.get_topic_display()),
            ('Name', message.full_name),
            ('Organisation', message.organization or '—'),
            ('Email', message.email),
            ('Phone', message.phone or '—'),
            ('Subject', message.subject),
            ('Message', message.message),
        ],
        f'shop/contactmessage/{message.pk}/change/',
        reply_to=[message.email],
    )


def send_contact_reply(message):
    return send_templated_email(
        'contact_reply', message.email, f'Re: {message.subject}', {'message': message},
        related_object=f'contact #{message.pk}',
    )


def send_lookup_code(customer, code):
    return send_templated_email(
        'lookup_code', customer.email, f'Your Zakayo Industries code: {code}',
        {'customer': customer, 'code': code, 'minutes': 10},
        related_object=f'customer #{customer.pk}',
    )


def send_custom_message(to, name, subject, body, related_object=''):
    return send_templated_email(
        'custom_message', to, subject, {'name': name, 'body': body}, related_object=related_object,
    )
