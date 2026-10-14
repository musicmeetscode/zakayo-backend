from celery import shared_task

from .emails import deliver
from .models import EmailLog


@shared_task(bind=True, max_retries=4, ignore_result=True)
def deliver_email(self, log_id):
    """Send a queued EmailLog. Retries with backoff (1, 2, 4, 8 min) when the mail server fails."""
    log = EmailLog.objects.filter(pk=log_id).first()
    if log is None or log.status == EmailLog.Status.SENT:
        return
    if not deliver(log) and self.request.retries < self.max_retries:
        # Stays visible as "failed" in the admin between attempts; a later success flips it to "sent".
        raise self.retry(countdown=60 * 2 ** self.request.retries)
