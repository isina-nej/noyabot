"""Shared research models. Mirrors botapp.web style: Pydantic schemas for
tool I/O plus plain dataclasses for normalized provider output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["post", "video", "comment", "channel", "article", "page"]
ResearchSource = Literal["web", "x", "youtube"]


@dataclass(slots=True)
class ResearchItem:
    platform: str  # web | x | youtube
    source_type: str  # post | video | comment | channel | article | page
    source_id: str | None
    author: str | None
    title: str | None
    text: str | None
    published_at: str | None
    url: str
    engagement: dict | None = None
    metadata: dict = field(default_factory=dict)
    score: float = 0.0

    def dedupe_key(self) -> str:
        from botapp.web.ranking import canonicalize_url

        url_key = canonicalize_url(self.url or "").lower()
        if url_key:
            return f"url:{url_key}"
        body = f"{self.title or ''}\n{self.text or ''}".strip().casefold()
        import hashlib

        return "body:" + hashlib.sha256(body.encode()[:4000]).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "author": self.author,
            "title": self.title,
            "text": self.text,
            "published_at": self.published_at,
            "url": self.url,
            "engagement": self.engagement,
            "metadata": self.metadata,
            "score": self.score,
        }


@dataclass(slots=True)
class ProviderStatus:
    name: str
    status: str  # ok | disabled | rate_limited | auth_required | timeout | not_found | provider_error | capability_unavailable
    count: int = 0
    duration_ms: float = 0.0
    error: str = ""


@dataclass(slots=True)
class ResearchResult:
    status: str  # ok | partial
    items: list[ResearchItem]
    providers: dict[str, ProviderStatus]
    evidence: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "items": [i.to_dict() for i in self.items],
            "providers": {k: {"status": v.status, "count": v.count} for k, v in self.providers.items()},
            "evidence": self.evidence,
            "truncated": self.truncated,
        }


# --- Pydantic tool schemas (strict, extra=ignore like agent schemas) ---


class XSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)
    language: str | None = Field(default=None, max_length=16)

    @field_validator("query")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:500]


class XPostParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    post_id: str = Field(min_length=1, max_length=64)

    @field_validator("post_id")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = (v or "").strip()
        m = __import__("re").search(r"(\d{8,25})", v)
        return m.group(1) if m else v[:64]


class XUserParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    handle: str = Field(min_length=1, max_length=64)

    @field_validator("handle")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip().lstrip("@")[:64]


class XUserPostsParams(XUserParams):
    limit: int = Field(default=10, ge=1, le=20)


class YouTubeSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=25)
    type: str = Field(default="video", max_length=16)
    channel_id: str | None = Field(default=None, max_length=64)
    published_after: str | None = Field(default=None, max_length=32)

    @field_validator("query")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:500]

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        v = (v or "video").strip().lower()
        return v if v in {"video", "channel", "playlist"} else "video"


class YouTubeVideoParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    video_id: str = Field(min_length=1, max_length=64)

    @field_validator("video_id")
    @classmethod
    def _trim(cls, v: str) -> str:
        import re

        v = (v or "").strip()
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,20})", v)
        if m:
            return m.group(1)
        m = re.search(r"([A-Za-z0-9_-]{11})", v)
        return m.group(1) if m else v[:64]


class YouTubeChannelParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    channel_id: str = Field(min_length=1, max_length=128)

    @field_validator("channel_id")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:128]


class YouTubeCommentsParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    video_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("video_id")
    @classmethod
    def _trim(cls, v: str) -> str:
        import re

        v = (v or "").strip()
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,20})", v)
        if m:
            return m.group(1)
        m = re.search(r"([A-Za-z0-9_-]{11})", v)
        return m.group(1) if m else v[:64]


class YouTubePlaylistParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    playlist_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=25)

    @field_validator("playlist_id")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:128]


class SocialSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=500)
    sources: list[str] = Field(default_factory=lambda: ["web", "x", "youtube"])
    time_range: str = Field(default="7d", max_length=16)
    depth: str = Field(default="standard", max_length=16)
    max_results_per_source: int = Field(default=8, ge=1, le=15)

    @field_validator("query")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:500]

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        clean = []
        for s in v or []:
            s = (s or "").strip().lower()
            if s in {"web", "x", "twitter", "youtube", "yt"}:
                clean.append("youtube" if s in {"yt"} else ("x" if s == "twitter" else s))
        seen = list(dict.fromkeys(clean))
        return seen[:3] or ["web", "x", "youtube"]

    @field_validator("time_range")
    @classmethod
    def _range(cls, v: str) -> str:
        v = (v or "7d").strip().lower()
        return v if v in {"24h", "7d", "30d", "any"} else "7d"

    @field_validator("depth")
    @classmethod
    def _depth(cls, v: str) -> str:
        v = (v or "standard").strip().lower()
        return v if v in {"quick", "standard", "deep"} else "standard"
