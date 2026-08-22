"""
API error handling.

One shape for every error the client can receive::

    {"error": {"type": "...", "message": "...", "detail": ...}}

so the frontend has exactly one branch for failure instead of guessing whether a
response is a DRF field-error dict, a plain string, or an HTML 500 page.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.llm.service import RegexResolutionError
from apps.llm.validation import RegexRejected
from apps.storage.s3 import ObjectNotFound, ObjectTooLarge, StorageError

logger = logging.getLogger(__name__)

_STATUS_MAP: tuple[tuple[type[Exception], int], ...] = (
    (ObjectNotFound, status.HTTP_404_NOT_FOUND),
    (ObjectTooLarge, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE),
    (RegexRejected, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (RegexResolutionError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (StorageError, status.HTTP_502_BAD_GATEWAY),
)


def error_payload(exc_type: str, message: str, detail=None) -> dict:
    body = {"error": {"type": exc_type, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return body


def api_exception_handler(exc, context):
    """DRF hook: map domain exceptions to sensible codes, normalise the body."""
    for exception_class, code in _STATUS_MAP:
        if isinstance(exc, exception_class):
            logger.warning("%s: %s", type(exc).__name__, exc)
            return Response(error_payload(type(exc).__name__, str(exc)), status=code)

    response = drf_exception_handler(exc, context)
    if response is None:
        # Unhandled: let Django's 500 machinery log the traceback, but still
        # answer with the documented shape.
        logger.exception("unhandled API error", exc_info=exc)
        return Response(
            error_payload("ServerError", "an unexpected error occurred"),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    data = response.data
    if isinstance(data, dict) and "error" in data and isinstance(data["error"], dict):
        return response

    message = "request could not be processed"
    detail = data
    if isinstance(data, dict) and "detail" in data:
        message, detail = str(data["detail"]), None
    response.data = error_payload(type(exc).__name__, message, detail)
    return response
