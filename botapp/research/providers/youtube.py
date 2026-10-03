"""YouTube Data API v3 research provider.

Supports search, video details, comments, playlists, channels, and
accurate transcript capability reporting. Never invents captions.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..credentials import CredentialStore
from ..errors import (
    AuthRequired,
    NotFound,
    ProviderError,
    ProviderTimeout,
    RateLimited,
)
from ..models import ResearchItem

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
MAX_RESULTS = 25
MAX_COMMENTS = 50
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _parse_duration(iso_duration: str | None) -> str:
    if not iso_duration:
        return ""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not m:
        return iso_duration
    h, m_val, s = m.groups()
    parts = []
    if h:
        parts.append(h)
    parts.append(m_val.zfill(2) if (h and m_val) else (m_val or "0"))
    parts.append((s or "0").zfill(2))
    return ":".join(parts)


class YouTubeResearchProvider:
    name = "youtube"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    def is_available(self) -> bool:
        if not CredentialStore.youtube_enabled():
            return False
        return bool(CredentialStore.youtube_api_key() or CredentialStore.youtube_refresh_token())

    def _api_key(self) -> str:
        key = CredentialStore.youtube_api_key()
        if not key:
            raise AuthRequired("YOUTUBE_API_KEY is not configured")
        return key

    def _handle_error(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            errors = data.get("error", {}).get("errors", [])
            reason = errors[0].get("reason", "") if errors else ""
            if "quota" in reason or "rateLimit" in reason:
                raise RateLimited(f"YouTube API quota exceeded: {reason}")
            raise AuthRequired(f"YouTube API auth rejected (status {resp.status_code}): {reason or 'check API key'}")
        if resp.status_code == 404:
            raise NotFound("YouTube resource not found")
        if resp.status_code == 429:
            raise RateLimited("YouTube API rate limit reached")
        if resp.status_code >= 400:
            raise ProviderError(f"YouTube API error (status {resp.status_code})")

    # --- Public API methods ---

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        type: str = "video",
        channel_id: str | None = None,
        published_after: str | None = None,
        **kwargs,
    ) -> list[ResearchItem]:
        limit = min(max(1, limit), MAX_RESULTS)
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        client = self._get_client()
        url = f"{YOUTUBE_API_BASE}/search"
        params: dict[str, Any] = {
            "key": self._api_key(),
            "part": "snippet",
            "q": clean_q,
            "maxResults": limit,
            "type": type if type in {"video", "channel", "playlist"} else "video",
        }
        if channel_id:
            params["channelId"] = channel_id.strip()
        if published_after:
            params["publishedAfter"] = published_after.strip()

        try:
            resp = await client.get(url, params=params)
            self._handle_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"YouTube search timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"YouTube search failed: {type(exc).__name__}") from exc

        out: list[ResearchItem] = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            id_info = item.get("id", {})
            vid = id_info.get("videoId")
            cid = id_info.get("channelId") or snippet.get("channelId")
            pid = id_info.get("playlistId")

            stype = "video" if vid else ("channel" if cid and not vid else ("playlist" if pid else "video"))
            sid = vid or cid or pid
            if vid:
                url_str = f"https://www.youtube.com/watch?v={vid}"
            elif cid:
                url_str = f"https://www.youtube.com/channel/{cid}"
            elif pid:
                url_str = f"https://www.youtube.com/playlist?list={pid}"
            else:
                url_str = "https://www.youtube.com"

            out.append(
                ResearchItem(
                    platform="youtube",
                    source_type=stype,
                    source_id=sid,
                    author=snippet.get("channelTitle"),
                    title=snippet.get("title"),
                    text=snippet.get("description"),
                    published_at=snippet.get("publishedAt"),
                    url=url_str,
                    engagement=None,
                    metadata={"channel_id": cid, "live_broadcast": snippet.get("liveBroadcastContent")},
                )
            )
        return out

    async def get_video(self, video_id: str) -> ResearchItem:
        video_id = (video_id or "").strip()
        if not video_id:
            raise NotFound("video_id is required")

        client = self._get_client()
        url = f"{YOUTUBE_API_BASE}/videos"
        params = {
            "key": self._api_key(),
            "part": "snippet,contentDetails,statistics",
            "id": video_id,
        }
        try:
            resp = await client.get(url, params=params)
            self._handle_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"YouTube get_video timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"YouTube get_video failed: {type(exc).__name__}") from exc

        items = data.get("items", [])
        if not items:
            raise NotFound(f"Video {video_id} not found")

        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        cdetails = item.get("contentDetails", {})

        engagement = {
            "views": int(stats.get("viewCount", 0) or 0),
            "likes": int(stats.get("likeCount", 0) or 0),
            "comments": int(stats.get("commentCount", 0) or 0),
        }
        duration = _parse_duration(cdetails.get("duration"))

        return ResearchItem(
            platform="youtube",
            source_type="video",
            source_id=video_id,
            author=snippet.get("channelTitle"),
            title=snippet.get("title"),
            text=snippet.get("description"),
            published_at=snippet.get("publishedAt"),
            url=f"https://www.youtube.com/watch?v={video_id}",
            engagement=engagement,
            metadata={
                "channel_id": snippet.get("channelId"),
                "duration": duration,
                "tags": (snippet.get("tags") or [])[:10],
                "category_id": snippet.get("categoryId"),
                "caption_available": cdetails.get("caption") == "true",
            },
        )

    async def get_channel(self, channel_id: str) -> ResearchItem:
        channel_id = (channel_id or "").strip()
        if not channel_id:
            raise NotFound("channel_id is required")

        client = self._get_client()
        url = f"{YOUTUBE_API_BASE}/channels"
        # Support for both ID and forUsername
        params: dict[str, Any] = {
            "key": self._api_key(),
            "part": "snippet,statistics",
        }
        if channel_id.startswith("UC"):
            params["id"] = channel_id
        else:
            params["forHandle"] = channel_id if channel_id.startswith("@") else f"@{channel_id}"

        try:
            resp = await client.get(url, params=params)
            self._handle_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"YouTube get_channel timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"YouTube get_channel failed: {type(exc).__name__}") from exc

        items = data.get("items", [])
        if not items:
            raise NotFound(f"Channel {channel_id} not found")

        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})

        return ResearchItem(
            platform="youtube",
            source_type="channel",
            source_id=item.get("id"),
            author=snippet.get("title"),
            title=snippet.get("title"),
            text=snippet.get("description"),
            published_at=snippet.get("publishedAt"),
            url=f"https://www.youtube.com/channel/{item.get('id')}",
            engagement={
                "subscribers": int(stats.get("subscriberCount", 0) or 0),
                "views": int(stats.get("viewCount", 0) or 0),
                "videos": int(stats.get("videoCount", 0) or 0),
            },
            metadata={"custom_url": snippet.get("customUrl"), "country": snippet.get("country")},
        )

    async def get_comments(self, video_id: str, *, limit: int = 10) -> list[ResearchItem]:
        video_id = (video_id or "").strip()
        if not video_id:
            return []
        limit = min(max(1, limit), MAX_COMMENTS)

        client = self._get_client()
        url = f"{YOUTUBE_API_BASE}/commentThreads"
        params = {
            "key": self._api_key(),
            "part": "snippet",
            "videoId": video_id,
            "maxResults": limit,
            "order": "relevance",
            "textFormat": "plainText",
        }
        try:
            resp = await client.get(url, params=params)
            self._handle_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"YouTube get_comments timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"YouTube get_comments failed: {type(exc).__name__}") from exc

        out: list[ResearchItem] = []
        for item in data.get("items", [])[:limit]:
            top = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            cid = item.get("id")
            out.append(
                ResearchItem(
                    platform="youtube",
                    source_type="comment",
                    source_id=cid,
                    author=top.get("authorDisplayName"),
                    title=None,
                    text=top.get("textDisplay"),
                    published_at=top.get("publishedAt"),
                    url=f"https://www.youtube.com/watch?v={video_id}&lc={cid}",
                    engagement={"likes": int(top.get("likeCount", 0) or 0)},
                    metadata={"video_id": video_id, "author_channel_id": top.get("authorChannelId", {}).get("value")},
                )
            )
        return out

    async def get_playlist(self, playlist_id: str, *, limit: int = 10) -> list[ResearchItem]:
        playlist_id = (playlist_id or "").strip()
        if not playlist_id:
            return []
        limit = min(max(1, limit), MAX_RESULTS)

        client = self._get_client()
        url = f"{YOUTUBE_API_BASE}/playlistItems"
        params = {
            "key": self._api_key(),
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": limit,
        }
        try:
            resp = await client.get(url, params=params)
            self._handle_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"YouTube get_playlist timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"YouTube get_playlist failed: {type(exc).__name__}") from exc

        out: list[ResearchItem] = []
        for item in data.get("items", [])[:limit]:
            snippet = item.get("snippet", {})
            vid = snippet.get("resourceId", {}).get("videoId")
            out.append(
                ResearchItem(
                    platform="youtube",
                    source_type="video",
                    source_id=vid,
                    author=snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle"),
                    title=snippet.get("title"),
                    text=snippet.get("description"),
                    published_at=snippet.get("publishedAt"),
                    url=f"https://www.youtube.com/watch?v={vid}" if vid else f"https://www.youtube.com/playlist?list={playlist_id}",
                    engagement=None,
                    metadata={"playlist_id": playlist_id, "position": snippet.get("position")},
                )
            )
        return out

    async def get_transcript(self, video_id: str) -> dict[str, Any]:
        """Fetch transcript accurately if available via timedtext or OAuth caption.

        Never fabricates transcripts. If unavailable, returns {available: False, reason: ...}.
        """
        video_id = (video_id or "").strip()
        if not video_id:
            return {"available": False, "reason": "video_id is required"}

        # First check caption metadata through video details
        try:
            vid_item = await self.get_video(video_id)
            has_caption = vid_item.metadata.get("caption_available", False)
        except Exception:
            has_caption = False

        # Attempt public timedtext caption track without third-party libraries
        client = self._get_client()
        for lang in ("fa", "en", "en-US"):
            try:
                tt_url = f"https://www.youtube.com/api/timedtext?v={video_id}&lang={lang}&fmt=json3"
                resp = await client.get(tt_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and resp.text.strip():
                    data = resp.json()
                    events = data.get("events", [])
                    lines = []
                    for ev in events:
                        segs = ev.get("segs", [])
                        text = "".join(s.get("utf8", "") for s in segs).strip()
                        if text:
                            lines.append(text)
                    if lines:
                        full_text = " ".join(lines)
                        return {
                            "available": True,
                            "language": lang,
                            "text": full_text[:8000],
                            "char_count": len(full_text),
                        }
            except Exception:
                pass

        return {
            "available": False,
            "video_id": video_id,
            "reason": "Transcript is not publicly available or requires authenticated caption download",
        }
