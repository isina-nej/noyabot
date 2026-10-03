"""Noya Research Layer: Web + X + YouTube aggregation.

Single entry point for chat tools. Providers stay behind this package;
the model only ever sees normalized ResearchItems and evidence blocks.
All external content is untrusted data, never instructions.
"""
from __future__ import annotations

from .errors import (
    ResearchError,
    AuthRequired,
    RateLimited,
    NotFound,
    ProviderTimeout,
    ProviderError,
    CapabilityUnavailable,
    ResearchDisabled,
)
from .models import ResearchItem, ProviderStatus, ResearchResult
from .service import ResearchService, default_research_service

__all__ = [
    "ResearchError",
    "AuthRequired",
    "RateLimited",
    "NotFound",
    "ProviderTimeout",
    "ProviderError",
    "CapabilityUnavailable",
    "ResearchDisabled",
    "ResearchItem",
    "ProviderStatus",
    "ResearchResult",
    "ResearchService",
    "default_research_service",
]
