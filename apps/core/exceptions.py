from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import ProtectedError, RestrictedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Turn model-level errors into clean 400 responses instead of 500s."""
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "error_dict") else {"detail": " ".join(exc.messages)}
        return Response(detail, status=status.HTTP_400_BAD_REQUEST)
    if isinstance(exc, (ProtectedError, RestrictedError)):
        return Response(
            {"detail": "This record is used by other records (meals, expenses or deposits) and cannot be deleted. "
                       "Deactivate it instead."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(exc, IntegrityError):
        return Response(
            {"detail": "This record conflicts with an existing one (duplicate or still in use)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return exception_handler(exc, context)
