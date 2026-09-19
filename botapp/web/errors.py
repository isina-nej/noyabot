"""Custom exceptions for Noya Web subsystem."""
from __future__ import annotations


class SearchError(Exception):
    """Base exception for search and web operations."""
    pass


class SearchProviderError(SearchError):
    """Raised when search provider fails or returns invalid response."""
    pass


class WebFetchError(SearchError):
    """Raised when HTTP fetch fails (network, status code, size limit)."""
    pass


class BlockedURLError(SearchError):
    """Raised when URL violates security policies (SSRF, non-http, private IP)."""
    pass


class ExtractionError(SearchError):
    """Raised when HTML parsing or content extraction fails."""
    pass


class SearchBudgetExceeded(SearchError):
    """Raised when search or fetch iteration budget is exceeded."""
    pass
