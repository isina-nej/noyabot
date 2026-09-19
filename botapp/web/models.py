"""Data models for Noya Web Intelligence Layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SearchDecision(str, Enum):
    """Decision on whether a query requires live web search."""
    NO_SEARCH = "no_search"
    SHOULD_SEARCH = "should_search"
    MUST_SEARCH = "must_search"


@dataclass(slots=True)
class SearchResult:
    """Represents a single search result from any search provider."""
    title: str
    url: str
    snippet: str
    source: str = "duckduckgo"
    rank: int = 0
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "rank": self.rank,
            "score": self.score,
        }


@dataclass(slots=True)
class WebDocument:
    """Structured document extracted from a web page."""
    url: str
    final_url: str
    title: str = ""
    description: str = ""
    author: str = ""
    published_at: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    text: str = ""
    language: str = "fa"
    content_type: str = "text/html"
    status_code: int = 200
    char_count: int = 0

    def __post_init__(self):
        if not self.char_count and self.text:
            self.char_count = len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "description": self.description,
            "author": self.author,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "text": self.text,
            "language": self.language,
            "content_type": self.content_type,
            "status_code": self.status_code,
            "char_count": self.char_count,
        }


@dataclass(slots=True)
class EvidenceChunk:
    """A relevant chunk of evidence extracted for LLM context."""
    source_id: str
    title: str
    url: str
    text: str
    score: float = 1.0


@dataclass(slots=True)
class SearchBudget:
    """Safety budget to prevent infinite loops and runaway bandwidth."""
    max_searches: int = 3
    max_fetches: int = 6
    max_total_bytes: int = 10 * 1024 * 1024  # 10 MB
    timeout_seconds: float = 25.0
    searches_done: int = 0
    fetches_done: int = 0
    bytes_consumed: int = 0

    def can_search(self) -> bool:
        return self.searches_done < self.max_searches

    def record_search(self) -> None:
        self.searches_done += 1

    def can_fetch(self) -> bool:
        return self.fetches_done < self.max_fetches

    def record_fetch(self, num_bytes: int) -> None:
        self.fetches_done += 1
        self.bytes_consumed += num_bytes


@dataclass
class SearchTrace:
    """Structured observability trace for each web operation."""
    request_id: str = ""
    chat_id: int | str = 0
    message_id: int | str = 0
    search_decision: SearchDecision = SearchDecision.NO_SEARCH
    search_reason: str = ""
    queries: list[str] = field(default_factory=list)
    provider: str = ""
    result_count: int = 0
    selected_results: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    fetch_failures: list[dict[str, str]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    final_source_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "search_decision": self.search_decision.value,
            "search_reason": self.search_reason,
            "queries": self.queries,
            "provider": self.provider,
            "result_count": self.result_count,
            "selected_results": self.selected_results,
            "fetched_urls": self.fetched_urls,
            "fetch_failures": self.fetch_failures,
            "tool_calls": self.tool_calls,
            "latency_ms": self.latency_ms,
            "final_source_count": self.final_source_count,
        }
