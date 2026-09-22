"""Tests for the Jev (SystemOne) media router (voice/image/none).

Fail-open contract: Jev must never break Noya. Disabled flag, missing key,
plain chat without media hints, HTTP errors, and low-confidence verdicts
all fall through to the existing LLM path.
"""

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from unittest.mock import AsyncMock, patch

from botapp.agent import jev_media, jev_router

_ENV = {
    "NOYA_API_KEY": "test-key",
    "NOYA_API_URL": "https://example.test/v1/chat/completions",
    "JEV_MODEL": "oc/jev-1.13-free",
    "JEV_FALLBACK_MODEL": "",
    "JEV_COOLDOWN_SECONDS": "0",
    "JEV_TIMEOUT": "5",
    "JEV_MEDIA_ENABLED": "true",
}


def _media_response(media="voice", confidence=0.95):
    return httpx.Response(
        200,
        json={
            "answers": {
                "media": {
                    "type": "choice",
                    "choice": media,
                    "confidence": confidence,
                }
            }
        },
        request=httpx.Request("POST", "https://example.test/v1/systemone"),
    )


def _client(*effects):
    client = AsyncMock()
    client.post.side_effect = list(effects)
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context, client


class JevMediaTests(SimpleTestCase):
    def setUp(self):
        jev_router._cool_until = 0.0

    def test_disabled_makes_no_request(self):
        env = dict(_ENV, JEV_MEDIA_ENABLED="false")
        with patch.dict("os.environ", env, clear=True):
            ctx, client = _client(_media_response())
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("ویس بده سلام", chat_id=1)
        assert res is None
        client.post.assert_not_called()

    def test_plain_chat_skips_network(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, client = _client(_media_response())
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("سلام چطوری", chat_id=1)
        assert res is None
        client.post.assert_not_called()

    def test_voice_confident(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, _ = _client(_media_response("voice", 0.95))
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("اینو ویس بده", chat_id=1)
        assert res is not None
        self.assertEqual(jev_media.media_action(res["media"], res["confidence"]), "voice")

    def test_voice_low_confidence_ignored(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, _ = _client(_media_response("voice", 0.50))
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("ویس بده", chat_id=1)
        assert res is not None
        self.assertIsNone(jev_media.media_action(res["media"], res["confidence"]))

    def test_image_confident(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, _ = _client(_media_response("image", 0.90))
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("عکس یک گربه بکش", chat_id=1)
        assert res is not None
        self.assertEqual(jev_media.media_action(res["media"], res["confidence"]), "image")

    def test_none_verdict_falls_through(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, _ = _client(_media_response("none", 0.99))
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("عکس پروفایلت چیه", chat_id=1)
        assert res is not None
        self.assertIsNone(jev_media.media_action(res["media"], res["confidence"]))

    def test_http_error_returns_none(self):
        with patch.dict("os.environ", _ENV, clear=True):
            ctx, _ = _client(httpx.HTTPError("boom"))
            with patch("botapp.agent.jev_media.httpx.AsyncClient", return_value=ctx):
                res = async_to_sync(jev_media.jev_classify_media)("ویس بده سلام", chat_id=1)
        assert res is None

    def test_strip_voice_trigger(self):
        self.assertEqual(jev_media.strip_media_trigger("ویس بده سلام", "voice"), "سلام")

    def test_strip_empty_returns_none(self):
        self.assertIsNone(jev_media.strip_media_trigger("ویس بده", "voice"))

    def test_strip_image_trigger(self):
        self.assertEqual(jev_media.strip_media_trigger("عکس یک گربه بکش", "image"), "گربه")
