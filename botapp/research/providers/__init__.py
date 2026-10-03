"""Base interface for research providers."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ResearchItem


@runtime_checkable
class ResearchProvider(Protocol):
    """Abstract interface for research providers (web, x, youtube)."""

    name: str

    async def search(self, query: str, *, limit: int = 10, **kwargs) -> list[ResearchItem]:
        """Execute search and return normalized ResearchItems."""
        ...

    def is_available(self) -> bool:
        """True if this provider has credentials configured."""
        ...
