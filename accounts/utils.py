import logging
import traceback

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .models import ErrorLog

logger = logging.getLogger(__name__)


class Util:
    @staticmethod
    def send_email(data):
        try:
            email = EmailMessage(
                to=[data['to_email']], subject=data['email_subject'], body=data['email_body'])
            email.send()
        except Exception:
            logger.exception("Failed to send email to %s", data.get('to_email'))


def _flatten_errors(data, prefix=''):
    """Turn DRF error data (dicts/lists of ErrorDetail) into a flat list of readable strings."""
    errors = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ('detail', 'non_field_errors'):
                field = prefix
            else:
                field = f"{prefix}.{key}" if prefix else key
            errors.extend(_flatten_errors(value, field))
    elif isinstance(data, list):
        for item in data:
            errors.extend(_flatten_errors(item, prefix))
    else:
        errors.append(f"{prefix}: {data}" if prefix else str(data))
    return errors


def _field_errors(data, prefix=''):
    """Map DRF validation errors to {"dotted.field.path": ["message", ...]} so clients can mark form fields."""
    fields = {}
    if isinstance(data, dict):
        for key, value in data.items():
            path = prefix if key in ('detail', 'non_field_errors') else (f"{prefix}.{key}" if prefix else key)
            for field, messages in _field_errors(value, path).items():
                fields.setdefault(field, []).extend(messages)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            path = f"{prefix}.{index}" if isinstance(item, (dict, list)) and prefix else prefix
            for field, messages in _field_errors(item, path).items():
                fields.setdefault(field, []).extend(messages)
    elif prefix:
        fields[prefix] = [str(data)]
    return fields


def custom_exception_handler(exc, context):
    """
    Return every error as {"message": str, "errors": [str, ...]} with the right status code.
    Validation errors also carry "field_errors": {"field.path": [str, ...]}.
    `services.renderers.Renderer` wraps this into the standard response envelope.
    """
    response = exception_handler(exc, context)

    if response is not None:
        errors = _flatten_errors(response.data)
        body = {'message': errors[0] if errors else '', 'errors': errors}
        field_errors = _field_errors(response.data)
        if field_errors:
            body['field_errors'] = field_errors
        response.data = body
        return response

    if isinstance(exc, IntegrityError):
        message = "A record with these details already exists."
        return Response({'message': message, 'errors': [message]}, status=status.HTTP_400_BAD_REQUEST)

    # Unhandled exception: log it, and only expose details in DEBUG.
    logger.exception("Unhandled API exception", exc_info=exc)
    try:
        ErrorLog.objects.create(
            level='ERROR',
            message=str(exc),
            details=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
    except Exception:
        logger.exception("Failed to write ErrorLog")

    message = str(exc) if settings.DEBUG else "Internal server error."
    return Response({'message': message, 'errors': [message]}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
