"""Pagination for list endpoints (the *result* pages are handled separately)."""
from rest_framework.pagination import PageNumberPagination

from django.conf import settings


class JobListPagination(PageNumberPagination):
    """
    Client-controllable page size, with a ceiling.

    ``page_size_query_param`` is a class attribute rather than a DRF setting, so
    without a subclass like this one a ``?page_size=`` parameter is accepted and
    then silently ignored.
    """

    page_size = 20
    page_size_query_param = "page_size"

    @property
    def max_page_size(self) -> int:
        return settings.API_MAX_PAGE_SIZE
