"""X/Twitter read-only bridge on 127.0.0.1:8791.

Routes:
  GET /health                    — liveness
  GET /me                        — authenticated user profile
  GET /user/<handle>/tweets      — user timeline (latest 20)
  GET /search?q=QUERY&n=20       — search posts
  GET /tweet/<id>                — single tweet by ID
"""
import json
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

PORT = 8791
COOKIE_FILE = Path(__file__).parent / "cookies.json"
BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# GraphQL features query string (same for all requests)
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


def load_cookies():
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def respond(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cookies = load_cookies()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        try:
            if path == "/health":
                self.respond(200, {"status": "ok"})

            elif path == "/me":
                # We don't know our own handle; use the authenticated
                # /settings endpoint as a liveness probe, and
                # rely on /user/<handle> for profile reads.
                r = x_get("https://x.com/i/api/1.1/help/settings.json", cookies)
                self.respond(r.status_code, r.json())

            elif "/tweets" in path:
                # /user/<handle>/tweets
                handle = path.split("/")[2]
                # First resolve user ID
                r = x_get(
                    f"https://x.com/i/api/graphql/G3KGOASz96M-Qu0nwmGXNg/UserByScreenName?"
                    f'variables=%7B%22screen_name%22%3A%22{handle}%22%7D&features={FEATURES}',
                    cookies,
                )
                uid = r.json()["data"]["user"]["result"]["rest_id"]
                # Then fetch tweets
                variables = json.dumps({
                    "userId": uid, "count": 20,
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": True,
                    "withVoice": True, "withV2Timeline": True,
                })
                from urllib.parse import quote as urlquote
                r2 = x_get(
                    f"https://x.com/i/api/graphql/H8OOoI-5ZE4NxgRr8lfyWg/UserTweets?"
                    f"variables={urlquote(variables)}&features={FEATURES}",
                    cookies,
                )
                self.respond(r2.status_code, r2.json())

            elif path.startswith("/tweet/"):
                tid = path.split("/")[2]
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
                r = x_get(
                    f"https://x.com/i/api/graphql/lZ0GCEojmtQfiUQa5oJSEw/SearchTimeline?"
                    f'variables=%7B%22rawQuery%22%3A%22{q}%22%2C%22count%22%3A{n}'
                    f'%2C%22querySource%22%3A%22typed_query%22%2C%22product%22%3A%22Latest%22%7D'
                    f"&features={FEATURES}",
                    cookies,
                )
                self.respond(r.status_code, r.json())

            else:
                self.respond(404, {"error": "not found"})

        except Exception as e:
            self.respond(502, {"error": str(e)})


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"X-bridge listening on http://127.0.0.1:{PORT}")
    server.serve_forever()
