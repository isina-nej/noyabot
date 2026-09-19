"""NoyaBot Web Intelligence Layer package."""
from __future__ import annotations

from .errors import (
    BlockedURLError,
    ExtractionError,
    SearchBudgetExceeded,
    SearchError,
    SearchProviderError,
    WebFetchError,
)
from .evidence import EvidenceBuilder
from .fetcher import StaticFetcher
from .models import (
    EvidenceChunk,
    SearchBudget,
    SearchDecision,
    SearchResult,
    SearchTrace,
    WebDocument,
)
from .policy import (
    extract_clean_search_query,
    extract_urls,
    has_url,
    normalize_persian_text,
    resolve_search_intent,
)
from .providers.base import SearchProvider
from .providers.duckduckgo import DuckDuckGoProvider
from .ranking import SearchResultRanker, canonicalize_url
from .security import is_ip_blocked, validate_url_security
from .service import WebService, default_web_service

__all__ = [
    "BlockedURLError",
    "EvidenceBuilder",
    "EvidenceChunk",
    "ExtractionError",
    "SearchBudget",
    "SearchBudgetExceeded",
    "SearchDecision",
    "SearchError",
    "SearchProvider",
    "SearchProviderError",
    "SearchResult",
    "SearchResultRanker",
    "SearchTrace",
    "StaticFetcher",
    "WebDocument",
    "WebFetchError",
    "WebService",
    "canonicalize_url",
    "default_web_service",
    "extract_clean_search_query",
    "extract_urls",
    "has_url",
    "is_ip_blocked",
    "normalize_persian_text",
    "resolve_search_intent",
    "validate_url_security",
]
