"""Base interface and protocol for search engine providers."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import SearchResult


@runtime_checkable
class SearchProvider(Protocol):
    """Abstract interface for all web search providers."""

    name: str

    async def search(self, query: str, limit: int = 8) -> list[SearchResult]:
        """Execute search for the given query and return normalized results."""
        ...
