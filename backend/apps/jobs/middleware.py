"""Request correlation: one id from the browser through to the worker logs."""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)

HEADER = "HTTP_X_REQUEST_ID"
RESPONSE_HEADER = "X-Request-ID"


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get(HEADER) or uuid.uuid4().hex[:12]
        request.request_id = request_id
        response = self.get_response(request)
        response[RESPONSE_HEADER] = request_id
        return response
