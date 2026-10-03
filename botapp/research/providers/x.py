"""X/Twitter research provider.

Routes through Official X API v2 when bearer token is set, else through
the local x-bridge if enabled. Returns strictly normalized ResearchItems.
Never leaks credentials into logs, output, or error messages.
"""
from __future__ import annotations

import logging

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
MAX_RESULTS = 20
MAX_THREAD_POSTS = 10


def _parse_metrics(public_metrics: dict | None) -> dict:
    pm = public_metrics or {}
    return {
        "likes": int(pm.get("like_count", 0) or 0),
        "reposts": int(pm.get("retweet_count", 0) or pm.get("repost_count", 0) or 0),
        "replies": int(pm.get("reply_count", 0) or 0),
        "quotes": int(pm.get("quote_count", 0) or 0),
    }


def _extract_legacy_tweet(entry: dict) -> dict | None:
    """Best-effort extraction from GraphQL timeline entry."""
    content = entry.get("content", {})
    item = content.get("itemContent", {})
    tweet_result = item.get("tweet_results", {}).get("result", {})
    if not tweet_result:
        # maybe tweet-in-tweet (quoted/repost)
        tweet_result = entry.get("tweet", {})
    legacy = tweet_result.get("legacy") or {}
    user_legacy = (
        tweet_result.get("core", {})
        .get("user_results", {})
        .get("result", {})
        .get("legacy", {})
    )
    tid = tweet_result.get("rest_id") or legacy.get("id_str")
    if not tid and not legacy.get("full_text"):
        return None
    return {
        "id": str(tid),
        "text": legacy.get("full_text", ""),
        "author": user_legacy.get("name") or "",
        "author_handle": user_legacy.get("screen_name") or "",
        "published_at": legacy.get("created_at"),
        "metrics": {
            "likes": legacy.get("favorite_count", 0),
            "reposts": legacy.get("retweet_count", 0),
            "replies": legacy.get("reply_count", 0),
        },
    }


class XResearchProvider:
    name = "x"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    def is_available(self) -> bool:
        if not CredentialStore.x_enabled():
            return False
        if CredentialStore.x_bearer():
            return True
        if CredentialStore.x_session_enabled() and CredentialStore.x_bridge_url():
            return True
        return False

    def active_backend(self) -> str:
        pref = CredentialStore.x_provider()
        if pref == "official" and CredentialStore.x_bearer():
            return "official"
        if pref == "session" and CredentialStore.x_session_enabled():
            return "session"
        if CredentialStore.x_bearer():
            return "official"
        if CredentialStore.x_session_enabled():
            return "session"
        return "none"

    # --- Public API methods ---

    async def search(self, query: str, *, limit: int = 10, **kwargs) -> list[ResearchItem]:
        limit = min(max(1, limit), MAX_RESULTS)
        clean_q = (query or "").strip()
        if not clean_q:
            return []
        backend = self.active_backend()
        if backend == "official":
            return await self._search_official(clean_q, limit=limit, **kwargs)
        if backend == "session":
            return await self._search_bridge(clean_q, limit=limit, **kwargs)
        raise AuthRequired("X credentials are not configured")

    async def get_post(self, post_id: str) -> ResearchItem:
        post_id = (post_id or "").strip()
        if not post_id:
            raise NotFound("post_id is required")
        backend = self.active_backend()
        if backend == "official":
            return await self._get_post_official(post_id)
        if backend == "session":
            return await self._get_post_bridge(post_id)
        raise AuthRequired("X credentials are not configured")

    async def get_thread(self, post_id: str, *, max_posts: int = MAX_THREAD_POSTS) -> list[ResearchItem]:
        post_id = (post_id or "").strip()
        if not post_id:
            return []
        max_posts = min(max(1, max_posts), MAX_THREAD_POSTS)
        backend = self.active_backend()
        if backend == "official":
            return await self._get_thread_official(post_id, max_posts=max_posts)
        if backend == "session":
            return await self._get_thread_bridge(post_id, max_posts=max_posts)
        raise AuthRequired("X credentials are not configured")

    async def get_user(self, handle: str) -> ResearchItem:
        handle = (handle or "").strip().lstrip("@")
        if not handle:
            raise NotFound("handle is required")
        backend = self.active_backend()
        if backend == "official":
            return await self._get_user_official(handle)
        if backend == "session":
            return await self._get_user_bridge(handle)
        raise AuthRequired("X credentials are not configured")

    async def get_user_posts(self, handle: str, *, limit: int = 10) -> list[ResearchItem]:
        handle = (handle or "").strip().lstrip("@")
        if not handle:
            return []
        limit = min(max(1, limit), MAX_RESULTS)
        backend = self.active_backend()
        if backend == "official":
            return await self._get_user_posts_official(handle, limit=limit)
        if backend == "session":
            return await self._get_user_posts_bridge(handle, limit=limit)
        raise AuthRequired("X credentials are not configured")

    async def get_replies(self, post_id: str, *, limit: int = 10) -> list[ResearchItem]:
        """Fetch direct replies to a tweet using conversation_id search or thread parse."""
        post_id = (post_id or "").strip()
        if not post_id:
            return []
        thread = await self.get_thread(post_id, max_posts=limit + 1)
        # Exclude the focal tweet itself
        return [p for p in thread if p.source_id != post_id][:limit]

    # --- Official API v2 implementation ---

    def _official_headers(self) -> dict[str, str]:
        bearer = CredentialStore.x_bearer()
        if not bearer:
            raise AuthRequired("X_BEARER_TOKEN is missing")
        return {"Authorization": f"Bearer {bearer}", "User-Agent": "NoyaBot-Research/1.0"}

    def _handle_http_error(self, resp: httpx.Response) -> None:
        if resp.status_code == 401 or resp.status_code == 403:
            raise AuthRequired(f"X API auth failed: status {resp.status_code}")
        if resp.status_code == 429:
            raise RateLimited("X API rate limit exceeded")
        if resp.status_code == 404:
            raise NotFound("Requested X resource was not found")
        if resp.status_code >= 400:
            raise ProviderError(f"X API error: status {resp.status_code}")

    async def _search_official(self, query: str, limit: int = 10, **kwargs) -> list[ResearchItem]:
        client = self._get_client()
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query": query,
            "max_results": min(max(10, limit), 100),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        try:
            resp = await client.get(url, params=params, headers=self._official_headers())
            self._handle_http_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"X search timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"X search failed: {type(exc).__name__}") from exc

        users_by_id = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        out: list[ResearchItem] = []
        for t in data.get("data", [])[:limit]:
            author = users_by_id.get(t.get("author_id"), {})
            handle = author.get("username", "")
            out.append(
                ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=t.get("id"),
                    author=author.get("name") or handle or None,
                    title=f"@{handle}" if handle else None,
                    text=t.get("text"),
                    published_at=t.get("created_at"),
                    url=f"https://x.com/{handle or 'i'}/status/{t.get('id')}",
                    engagement=_parse_metrics(t.get("public_metrics")),
                    metadata={"author_handle": handle, "conversation_id": t.get("conversation_id")},
                )
            )
        return out

    async def _get_post_official(self, post_id: str) -> ResearchItem:
        client = self._get_client()
        url = f"https://api.twitter.com/2/tweets/{post_id}"
        params = {
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        try:
            resp = await client.get(url, params=params, headers=self._official_headers())
            self._handle_http_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"X post timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"X get_post failed: {type(exc).__name__}") from exc

        t = data.get("data")
        if not t:
            raise NotFound(f"Post {post_id} not found")
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        author = users.get(t.get("author_id"), {})
        handle = author.get("username", "")
        return ResearchItem(
            platform="x",
            source_type="post",
            source_id=t.get("id"),
            author=author.get("name") or handle or None,
            title=f"@{handle}" if handle else None,
            text=t.get("text"),
            published_at=t.get("created_at"),
            url=f"https://x.com/{handle or 'i'}/status/{t.get('id')}",
            engagement=_parse_metrics(t.get("public_metrics")),
            metadata={"author_handle": handle, "conversation_id": t.get("conversation_id")},
        )

    async def _get_thread_official(self, post_id: str, max_posts: int = MAX_THREAD_POSTS) -> list[ResearchItem]:
        focal = await self._get_post_official(post_id)
        conv_id = focal.metadata.get("conversation_id") or post_id
        # Search recent tweets in same conversation
        client = self._get_client()
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query": f"conversation_id:{conv_id}",
            "max_results": min(max(10, max_posts), 100),
            "tweet.fields": "author_id,created_at,public_metrics,conversation_id",
            "expansions": "author_id",
            "user.fields": "name,username",
        }
        thread = [focal]
        try:
            resp = await client.get(url, params=params, headers=self._official_headers())
            if resp.status_code == 200:
                data = resp.json()
                users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
                for t in data.get("data", []):
                    if t.get("id") == post_id:
                        continue
                    author = users.get(t.get("author_id"), {})
                    handle = author.get("username", "")
                    thread.append(
                        ResearchItem(
                            platform="x",
                            source_type="post",
                            source_id=t.get("id"),
                            author=author.get("name") or handle or None,
                            title=f"@{handle}" if handle else None,
                            text=t.get("text"),
                            published_at=t.get("created_at"),
                            url=f"https://x.com/{handle or 'i'}/status/{t.get('id')}",
                            engagement=_parse_metrics(t.get("public_metrics")),
                            metadata={"author_handle": handle},
                        )
                    )
                    if len(thread) >= max_posts:
                        break
        except Exception as exc:
            logger.warning("X conversation search failed: %s", exc)
        return thread[:max_posts]

    async def _get_user_official(self, handle: str) -> ResearchItem:
        client = self._get_client()
        url = f"https://api.twitter.com/2/users/by/username/{handle}"
        params = {"user.fields": "description,created_at,public_metrics,verified"}
        try:
            resp = await client.get(url, params=params, headers=self._official_headers())
            self._handle_http_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"X user timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"X get_user failed: {type(exc).__name__}") from exc

        u = data.get("data")
        if not u:
            raise NotFound(f"User @{handle} not found")
        pm = u.get("public_metrics") or {}
        return ResearchItem(
            platform="x",
            source_type="channel",
            source_id=u.get("id"),
            author=u.get("name"),
            title=f"@{u.get('username')}",
            text=u.get("description"),
            published_at=u.get("created_at"),
            url=f"https://x.com/{u.get('username')}",
            engagement={
                "followers": pm.get("followers_count", 0),
                "following": pm.get("following_count", 0),
                "tweets": pm.get("tweet_count", 0),
            },
            metadata={"username": u.get("username"), "verified": u.get("verified", False)},
        )

    async def _get_user_posts_official(self, handle: str, limit: int = 10) -> list[ResearchItem]:
        user = await self._get_user_official(handle)
        uid = user.source_id
        client = self._get_client()
        url = f"https://api.twitter.com/2/users/{uid}/tweets"
        params = {
            "max_results": min(max(5, limit), 100),
            "tweet.fields": "created_at,public_metrics",
        }
        try:
            resp = await client.get(url, params=params, headers=self._official_headers())
            self._handle_http_error(resp)
            data = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"X user posts timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"X get_user_posts failed: {type(exc).__name__}") from exc

        out: list[ResearchItem] = []
        for t in data.get("data", [])[:limit]:
            out.append(
                ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=t.get("id"),
                    author=user.author,
                    title=f"@{handle}",
                    text=t.get("text"),
                    published_at=t.get("created_at"),
                    url=f"https://x.com/{handle}/status/{t.get('id')}",
                    engagement=_parse_metrics(t.get("public_metrics")),
                    metadata={"author_handle": handle},
                )
            )
        return out

    # --- Session Bridge (x-bridge) implementation ---

    def _bridge_headers(self) -> dict[str, str]:
        token = CredentialStore.x_bridge_token()
        headers = {"User-Agent": "NoyaBot-Research/1.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _bridge_get(self, path: str, params: dict | None = None) -> dict:
        client = self._get_client()
        base = CredentialStore.x_bridge_url().rstrip("/")
        url = f"{base}{path}"
        try:
            resp = await client.get(url, params=params, headers=self._bridge_headers())
            if resp.status_code in (401, 403):
                raise AuthRequired(f"Bridge authentication rejected (status {resp.status_code})")
            if resp.status_code == 404:
                raise NotFound(f"Bridge resource not found: {path}")
            if resp.status_code == 429:
                raise RateLimited("Bridge reports rate limit exceeded")
            if resp.status_code >= 400:
                raise ProviderError(f"Bridge returned status {resp.status_code}")
            return resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"Bridge connection timed out") from exc
        except (AuthRequired, RateLimited, NotFound, ProviderError):
            raise
        except Exception as exc:
            raise ProviderError(f"Bridge request failed: {type(exc).__name__}") from exc

    async def _search_bridge(self, query: str, limit: int = 10, **kwargs) -> list[ResearchItem]:
        data = await self._bridge_get("/search", params={"q": query, "n": str(limit)})
        out: list[ResearchItem] = []
        instructions = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        entries: list[dict] = []
        for inst in instructions:
            if inst.get("type") == "TimelineAddEntries":
                entries.extend(inst.get("entries", []))

        for entry in entries:
            parsed = _extract_legacy_tweet(entry)
            if not parsed or not parsed.get("id"):
                continue
            handle = parsed.get("author_handle", "")
            out.append(
                ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=parsed["id"],
                    author=parsed.get("author") or handle or None,
                    title=f"@{handle}" if handle else None,
                    text=parsed.get("text"),
                    published_at=parsed.get("published_at"),
                    url=f"https://x.com/{handle or 'i'}/status/{parsed['id']}",
                    engagement=parsed.get("metrics"),
                    metadata={"author_handle": handle},
                )
            )
            if len(out) >= limit:
                break
        return out

    async def _get_post_bridge(self, post_id: str) -> ResearchItem:
        data = await self._bridge_get(f"/tweet/{post_id}")
        # Parse TweetDetail GraphQL response
        instructions = (
            data.get("data", {})
            .get("threaded_conversation_with_injections_v2", {})
            .get("instructions", [])
        )
        entries: list[dict] = []
        for inst in instructions:
            if inst.get("type") == "TimelineAddEntries":
                entries.extend(inst.get("entries", []))

        for entry in entries:
            parsed = _extract_legacy_tweet(entry)
            if parsed and parsed.get("id") == post_id:
                handle = parsed.get("author_handle", "")
                return ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=parsed["id"],
                    author=parsed.get("author") or handle or None,
                    title=f"@{handle}" if handle else None,
                    text=parsed.get("text"),
                    published_at=parsed.get("published_at"),
                    url=f"https://x.com/{handle or 'i'}/status/{parsed['id']}",
                    engagement=parsed.get("metrics"),
                    metadata={"author_handle": handle},
                )
        raise NotFound(f"Post {post_id} not found in bridge response")

    async def _get_thread_bridge(self, post_id: str, max_posts: int = MAX_THREAD_POSTS) -> list[ResearchItem]:
        data = await self._bridge_get(f"/tweet/{post_id}")
        instructions = (
            data.get("data", {})
            .get("threaded_conversation_with_injections_v2", {})
            .get("instructions", [])
        )
        entries: list[dict] = []
        for inst in instructions:
            if inst.get("type") == "TimelineAddEntries":
                entries.extend(inst.get("entries", []))

        items: list[ResearchItem] = []
        for entry in entries:
            parsed = _extract_legacy_tweet(entry)
            if not parsed or not parsed.get("id"):
                continue
            handle = parsed.get("author_handle", "")
            items.append(
                ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=parsed["id"],
                    author=parsed.get("author") or handle or None,
                    title=f"@{handle}" if handle else None,
                    text=parsed.get("text"),
                    published_at=parsed.get("published_at"),
                    url=f"https://x.com/{handle or 'i'}/status/{parsed['id']}",
                    engagement=parsed.get("metrics"),
                    metadata={"author_handle": handle},
                )
            )
            if len(items) >= max_posts:
                break
        return items

    async def _get_user_bridge(self, handle: str) -> ResearchItem:
        # UserByScreenName proxy in bridge
        posts = await self._get_user_posts_bridge(handle, limit=1)
        author = posts[0].author if posts else handle
        return ResearchItem(
            platform="x",
            source_type="channel",
            source_id=handle,
            author=author,
            title=f"@{handle}",
            text=f"X profile for @{handle}",
            published_at=None,
            url=f"https://x.com/{handle}",
            engagement=None,
            metadata={"handle": handle},
        )

    async def _get_user_posts_bridge(self, handle: str, limit: int = 10) -> list[ResearchItem]:
        data = await self._bridge_get(f"/user/{handle}/tweets")
        instructions = (
            data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline_v2", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        entries: list[dict] = []
        for inst in instructions:
            if inst.get("type") == "TimelineAddEntries":
                entries.extend(inst.get("entries", []))

        items: list[ResearchItem] = []
        for entry in entries:
            parsed = _extract_legacy_tweet(entry)
            if not parsed or not parsed.get("id"):
                continue
            h = parsed.get("author_handle") or handle
            items.append(
                ResearchItem(
                    platform="x",
                    source_type="post",
                    source_id=parsed["id"],
                    author=parsed.get("author") or h,
                    title=f"@{h}",
                    text=parsed.get("text"),
                    published_at=parsed.get("published_at"),
                    url=f"https://x.com/{h}/status/{parsed['id']}",
                    engagement=parsed.get("metrics"),
                    metadata={"author_handle": h},
                )
            )
            if len(items) >= limit:
                break
        return items
