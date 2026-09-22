"""Tests for the Jev (SystemOne) fast-path router.

Fail-open contract: Jev must never break Noya. Disabled flag, missing key,
HTTP errors, bad payloads, and a dead/expired subscription all fall through
to the existing LLM classifier, then to ordinary Noya chat.
"""

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from unittest.mock import AsyncMock, patch

from botapp.agent import jev_router
from botapp.agent.cache import classify_cache
from botapp.agent.classify import should_route_to_agent

_ENV = {
    "NOYA_API_KEY": "test-key",
    "NOYA_API_URL": "https://example.test/v1/chat/completions",
    "JEV_MODEL": "oc/jev-1.13-free",
    "JEV_FALLBACK_MODEL": "",
    "JEV_COOLDOWN_SECONDS": "0",
    "JEV_TIMEOUT": "5",
}


def _systemone_response(route="agent", confidence=0.95):
    return httpx.Response(
        200,
        json={
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": route,
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


class _Boom:
    """LLM provider double that explodes if touched (proves Jev short-circuit)."""

    async def classify_route(self, text, *, chat_id):
        raise AssertionError("LLM classifier must not be called on Jev fast path")


class _Llm:
    def __init__(self, route="agent", confidence=0.95):
        self._decision = {
            "route": route,
            "confidence": confidence,
            "thinking": "",
            "reason": "test",
        }

    async def classify_route(self, text, *, chat_id):
        return dict(self._decision)


class JevRouterTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_disabled_makes_no_request(self):
        env = dict(_ENV, JEV_ENABLED="false")
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient"
            ) as client_cls:
                result = async_to_sync(jev_router.jev_classify_route)(
                    "بن بده", chat_id=-1
                )
        self.assertIsNone(result)
        client_cls.assert_not_called()

    def test_missing_key_makes_no_request(self):
        env = dict(_ENV, NOYA_API_KEY="")
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient"
            ) as client_cls:
                result = async_to_sync(jev_router.jev_classify_route)(
                    "بن بده", chat_id=-1
                )
        self.assertIsNone(result)
        client_cls.assert_not_called()

    def test_agent_verdict(self):
        context, client = _client(_systemone_response("agent", 0.95))
        with patch.dict("os.environ", _ENV, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient",
                return_value=context,
            ):
                result = async_to_sync(jev_router.jev_classify_route)(
                    "بن بده", chat_id=-1
                )
        assert result is not None
        self.assertEqual(result["route"], "agent")
        self.assertAlmostEqual(result["confidence"], 0.95)
        post_url = client.post.await_args.args[0]
        self.assertTrue(post_url.endswith("/v1/systemone"))
        body = client.post.await_args.kwargs["json"]
        self.assertEqual(body["model"], "oc/jev-1.13-free")

    def test_fallback_model_tried_when_primary_fails(self):
        env = dict(
            _ENV,
            JEV_MODEL="oc/jev-1.13-free",
            JEV_FALLBACK_MODEL="openrouter/typesafe/jev-1.13",
        )
        context, client = _client(
            RuntimeError("free tier down"),
            _systemone_response("chat", 0.9),
        )
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient",
                return_value=context,
            ):
                result = async_to_sync(jev_router.jev_classify_route)(
                    "سلام", chat_id=-1
                )
        assert result is not None
        self.assertEqual(result["route"], "chat")
        self.assertEqual(client.post.await_count, 2)
        models = [
            call.kwargs["json"]["model"]
            for call in client.post.await_args_list
        ]
        self.assertEqual(
            models, ["oc/jev-1.13-free", "openrouter/typesafe/jev-1.13"]
        )

    def test_all_fail_returns_none_then_cools_down(self):
        env = dict(_ENV, JEV_COOLDOWN_SECONDS="60")
        context, client = _client(RuntimeError("down"))
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient",
                return_value=context,
            ) as client_cls:
                first = async_to_sync(jev_router.jev_classify_route)(
                    "سلام", chat_id=-1
                )
                client_cls.reset_mock()
                second = async_to_sync(jev_router.jev_classify_route)(
                    "سلام دوباره", chat_id=-1
                )
        self.assertIsNone(first)
        self.assertIsNone(second)
        client_cls.assert_not_called()

    def test_bad_payload_shape_returns_none(self):
        bad = httpx.Response(
            200,
            json={"unexpected": True},
            request=httpx.Request("POST", "https://example.test/v1/systemone"),
        )
        context, _ = _client(bad)
        with patch.dict("os.environ", _ENV, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient",
                return_value=context,
            ):
                result = async_to_sync(jev_router.jev_classify_route)(
                    "سلام", chat_id=-1
                )
        self.assertIsNone(result)


class JevClassifyIntegrationTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_jev_agent_confident_skips_llm(self):
        verdict = {"route": "agent", "confidence": 0.95, "thinking": "", "reason": "jev"}
        with patch(
            "botapp.agent.jev_router.jev_classify_route",
            new=AsyncMock(return_value=verdict),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42, provider=_Boom()
            )
        self.assertTrue(ok)

    def test_jev_chat_confident_skips_llm(self):
        verdict = {"route": "chat", "confidence": 0.9, "thinking": "", "reason": "jev"}
        with patch(
            "botapp.agent.jev_router.jev_classify_route",
            new=AsyncMock(return_value=verdict),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "خواب مفید چند تا چنده", chat_id=-991, provider=_Boom()
            )
        self.assertFalse(ok)

    def test_jev_down_falls_back_to_llm(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_route",
            new=AsyncMock(return_value=None),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42, provider=_Llm("agent", 0.95)
            )
        self.assertTrue(ok)

    def test_jev_ambiguous_falls_back_to_llm(self):
        verdict = {"route": "chat", "confidence": 0.5, "thinking": "", "reason": "jev"}
        with patch(
            "botapp.agent.jev_router.jev_classify_route",
            new=AsyncMock(return_value=verdict),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42, provider=_Llm("chat", 0.9)
            )
        self.assertFalse(ok)

    def test_kill_switch_leaves_old_path_untouched(self):
        env = dict(_ENV, JEV_ENABLED="false")
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "botapp.agent.jev_router.httpx.AsyncClient"
            ) as client_cls:
                ok = async_to_sync(should_route_to_agent)(
                    "یه کار عجیب مدیریتی بکن",
                    chat_id=-42,
                    provider=_Llm("agent", 0.95),
                )
        self.assertTrue(ok)
        client_cls.assert_not_called()
