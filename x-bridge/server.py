"""X/Twitter read-only bridge on 127.0.0.1:8791 (Hardened).

Security rules enforced:
  1. Internal-only: binds to 127.0.0.1 (never 0.0.0.0).
  2. Optional Bearer token auth via X_BRIDGE_INTERNAL_TOKEN (401 on mismatch).
  3. No CORS wildcard (local-only callers).
  4. Cookies never logged or returned in responses.
  5. Response size capped.
  6. Request parameters sanitized and encoded.

Routes:
  GET /health                    — liveness
  GET /me                        — authenticated user profile
  GET /user/<handle>/tweets      — user timeline (latest 20)
  GET /search?q=QUERY&n=20       — search posts
  GET /tweet/<id>                — single tweet by ID
"""
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote as urlquote, urlparse

import httpx

PORT = int(os.getenv("X_BRIDGE_PORT", "8791"))
COOKIE_FILE = Path(__file__).parent / "cookies.json"
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

FEATURES = (
    "%7B%22hidden_profile_subscriptions_enabled%22%3Atrue"
    "%2C%22rweb_tipjar_consumption_enabled%22%3Atrue"
    "%2C%22responsive_web_graphql_exclude_directive_enabled%22%3Atrue"
    "%2C%22verified_phone_label_enabled%22%3Afalse"
    "%2C%22subscriptions_verification_info_is_identity_verified_enabled%22%3Atrue"
    "%2C%22subscriptions_verification_info_verified_since_enabled%22%3Atrue"
    "%2C%22highlights_tweets_tab_ui_enabled%22%3Atrue"
    "%2C%22responsive_web_twitter_article_notes_tab_enabled%22%3Atrue"
    "%2C%22subscriptions_feature_can_gift_premium%22%3Atrue"
    "%2C%22creator_subscriptions_tweet_preview_api_enabled%22%3Atrue"
    "%2C%22responsive_web_graphql_skip_user_profile_image_extensions_enabled%22%3Afalse"
    "%2C%22responsive_web_graphql_timeline_navigation_enabled%22%3Atrue%7D"
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB cap
logger = logging.getLogger("x-bridge")


def load_cookies():
    if not COOKIE_FILE.exists():
        return {}
    with open(COOKIE_FILE) as f:
        return json.load(f)


def make_headers(cookies):
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "Authorization": f"Bearer {BEARER}",
        "Cookie": cookie_str,
        "X-Csrf-Token": cookies.get("ct0", ""),
        "User-Agent": UA,
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
    }


def x_get(url, cookies):
    with httpx.Client(http2=False, follow_redirects=True, timeout=15) as c:
        return c.get(url, headers=make_headers(cookies))


def is_authorized(headers) -> bool:
    token = os.getenv("X_BRIDGE_INTERNAL_TOKEN", "").strip()
    if not token:
        return True  # Auth not required if internal token not set
    auth = headers.get("Authorization", "")
    expected = f"Bearer {token}"
    return auth == expected


def sanitize_handle(h: str) -> str:
    cleaned = (h or "").strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9_]{1,32}$", cleaned):
        raise ValueError("invalid handle")
    return cleaned


def sanitize_id(tid: str) -> str:
    cleaned = (tid or "").strip()
    if not re.match(r"^\d{1,32}$", cleaned):
        raise ValueError("invalid id")
    return cleaned


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Never log cookies or query values
        pass

    def respond(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        if len(body) > MAX_RESPONSE_BYTES:
            body = json.dumps({"error": "response size exceeded limit"}).encode()
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # No wildcard CORS: this bridge is loopback-only
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        # Health endpoint bypasses auth
        if path == "/health":
            self.respond(200, {"status": "ok"})
            return

        if not is_authorized(self.headers):
            self.respond(401, {"error": "unauthorized"})
            return

        cookies = load_cookies()
        if not cookies:
            self.respond(503, {"error": "cookies not configured"})
            return

        try:
            if path == "/me":
                r = x_get("https://x.com/i/api/1.1/help/settings.json", cookies)
                self.respond(r.status_code, r.json())

            elif "/tweets" in path:
                parts = path.split("/")
                if len(parts) < 4:
                    self.respond(400, {"error": "invalid user path"})
                    return
                handle = sanitize_handle(parts[2])
                r = x_get(
                    f"https://x.com/i/api/graphql/G3KGOASz96M-Qu0nwmGXNg/UserByScreenName?"
                    f'variables=%7B%22screen_name%22%3A%22{handle}%22%7D&features={FEATURES}',
                    cookies,
                )
                uid = r.json()["data"]["user"]["result"]["rest_id"]
                variables = json.dumps({
                    "userId": uid, "count": 20,
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": True,
                    "withVoice": True, "withV2Timeline": True,
                })
                r2 = x_get(
                    f"https://x.com/i/api/graphql/H8OOoI-5ZE4NxgRr8lfyWg/UserTweets?"
                    f"variables={urlquote(variables)}&features={FEATURES}",
                    cookies,
                )
                self.respond(r2.status_code, r2.json())

            elif path.startswith("/tweet/"):
                parts = path.split("/")
                if len(parts) < 3:
                    self.respond(400, {"error": "invalid tweet path"})
                    return
                tid = sanitize_id(parts[2])
                r = x_get(
                    f"https://x.com/i/api/graphql/Gg49F49V0FWJ0UQ5pZks7g/TweetDetail?"
                    f'variables=%7B%22focalTweetId%22%3A%22{tid}%22%2C%22withCommunity%22%3Afalse%7D'
                    f"&features={FEATURES}",
                    cookies,
                )
                self.respond(r.status_code, r.json())

            elif path == "/search":
                q = qs.get("q", [""])[0]
                n = qs.get("n", ["20"])[0]
                try:
                    n_int = min(max(1, int(n)), 50)
                except ValueError:
                    n_int = 20
                safe_q = urlquote(q[:500])
                r = x_get(
                    f"https://x.com/i/api/graphql/lZ0GCEojmtQfiUQa5oJSEw/SearchTimeline?"
                    f'variables=%7B%22rawQuery%22%3A%22{safe_q}%22%2C%22count%22%3A{n_int}'
                    f'%2C%22querySource%22%3A%22typed_query%22%2C%22product%22%3A%22Latest%22%7D'
                    f"&features={FEATURES}",
                    cookies,
                )
                self.respond(r.status_code, r.json())

            else:
                self.respond(404, {"error": "not found"})

        except ValueError as e:
            self.respond(400, {"error": str(e)})
        except Exception as e:
            self.respond(502, {"error": type(e).__name__})


def run_bridge(host: str = "127.0.0.1", port: int = PORT):
    # Enforce loopback address strictly
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("X-bridge can only bind to loopback address")
    server = HTTPServer((host, port), Handler)
    print(f"X-bridge listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_bridge()
