"""Behavior tests for JEV-first 4-way routing.

Route selection is model-first; the deterministic parser is last-resort
validation only — never the primary selector.

Each test mocks the model layer (``jev_classify_request`` and/or the
provider doubles) and exercises the real routing + execution path:

- model routes to chat / admin / harness / media;
- low confidence (< 0.80) forbids tool execution → chat/clarification;
- reply-photo analysis wins over media (UNTRUSTED, no image edit);
- permission denial still fires after an admin verdict (Security Gate kept);
- a harness verdict runs the real harness bridge chain;
- JEV-down falls back safely to the structured/model → deterministic path.
"""

from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings

from botapp.agent import jev_router
from botapp.agent.cache import classify_cache
from botapp.agent.classify import should_route_to_agent
from botapp.agent.permissions import clear_role_cache
from botapp.agent.route_classifier import (
    ROUTE_CONFIDENCE_THRESHOLD,
    RouteVerdict,
    classify_request,
    is_tool_execution_allowed,
)
from botapp.models import GroupSettings

from .test_agent import _member, admin_bot, make_message


class _Structured:
    """Provider double emitting a 4-way structured verdict (data only)."""

    def __init__(self, route="chat", confidence=0.9, intent="chat",
                 target_source="none", reason="test"):
        self._verdict = {
            "route": route,
            "intent": intent,
            "confidence": confidence,
            "target_source": target_source,
            "reason": reason,
        }
        self.calls = 0

    async def classify_request(self, text, *, chat_id, has_reply=False):
        self.calls += 1
        return dict(self._verdict)


class _LegacyLlm:
    def __init__(self, route="agent", confidence=0.95):
        self._decision = {"route": route, "confidence": confidence,
                          "thinking": "", "reason": "test"}
        self.calls = 0

    async def classify_route(self, text, *, chat_id):
        self.calls += 1
        return dict(self._decision)


class _Boom:
    async def classify_request(self, *a, **k):
        raise AssertionError("provider must not be called on JEV short-circuit")

    async def classify_route(self, *a, **k):
        raise AssertionError("provider must not be called on JEV short-circuit")


def _jev(**kw):
    verdict = {"route": "chat", "confidence": 0.9}
    verdict.update(kw)
    return verdict


class ModelRoutesToChatTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_model_chat_verdict_stays_in_chat(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="chat", confidence=0.95)),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "سلام خوبی؟", chat_id=-1, provider=_Boom()
            )
        self.assertFalse(ok)


class ModelRoutesToAdminTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_model_admin_verdict_routes_to_agent(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="admin", confidence=0.95)),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه کار مدیریتی بکن", chat_id=-42, provider=_Boom()
            )
        self.assertTrue(ok)

    def test_structured_provider_admin_without_jev(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=None),
        ):
            provider = _Structured(route="admin", confidence=0.9,
                                   intent="admin_command")
            ok = async_to_sync(should_route_to_agent)(
                "یه کار مدیریتی بکن", chat_id=-42, provider=provider
            )
        self.assertTrue(ok)
        self.assertEqual(provider.calls, 1)


class ModelRoutesToHarnessTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_model_harness_verdict_routes_to_agent(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="harness", confidence=0.92)),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "بررسی کامل گروه", chat_id=-42, provider=_Boom()
            )
        self.assertTrue(ok)


class ModelRoutesToMediaTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_model_media_verdict_stays_in_chat(self):
        # media has its own image path; it must never route to admin tools.
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="media", confidence=0.95)),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "عکس یک گربه بکش", chat_id=-42, provider=_Boom()
            )
        self.assertFalse(ok)


class LowConfidenceBlocksToolsTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_low_conf_admin_verdict_does_not_execute(self):
        verdict = RouteVerdict(route="admin", intent="admin_command",
                               confidence=0.5, target_source="none", reason="t")
        self.assertLess(verdict.confidence, ROUTE_CONFIDENCE_THRESHOLD)
        self.assertFalse(is_tool_execution_allowed(verdict))

    def test_low_conf_harness_verdict_does_not_execute(self):
        verdict = RouteVerdict(route="harness", intent="harness_investigate",
                               confidence=0.6, target_source="none", reason="t")
        self.assertFalse(is_tool_execution_allowed(verdict))

    def test_confident_admin_verdict_allows(self):
        verdict = RouteVerdict(route="admin", intent="admin_command",
                               confidence=0.95, target_source="none", reason="t")
        self.assertTrue(is_tool_execution_allowed(verdict))

    def test_jev_low_conf_falls_through_to_second_opinion(self):
        # JEV says admin@0.5 → no execution; provider second opinion says
        # chat → stays in chat.
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="admin", confidence=0.5)),
        ):
            provider = _Structured(route="chat", confidence=0.9)
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42, provider=provider
            )
        self.assertFalse(ok)
        self.assertEqual(provider.calls, 1)

    def test_invalid_verdict_shape_rejected(self):
        # Pydantic forbid: extra tool field must raise, never validate.
        # The model can never smuggle a tool call, params, or target id.
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            RouteVerdict(route="admin", intent="x", confidence=0.9,
                         target_source="none", reason="t", tool="member.ban")
        with self.assertRaises(ValidationError):
            RouteVerdict(route="admin", intent="x", confidence=0.9,
                         target_source="none", reason="t",
                         target_user_id=123)


class ReplyPhotoAnalysisBeatsMediaTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_analysis_of_replied_photo_is_chat_not_media(self):
        verdict = async_to_sync(classify_request)(
            "این عکس را از نظر امنیتی بررسی کن؛ فقط تحلیل کن.",
            chat_id=-1,
            provider=_Boom(),
            has_reply=True,
            use_jev=True,
        )
        assert verdict is not None
        self.assertEqual(verdict.route, "chat")
        self.assertEqual(verdict.intent, "message_analysis")

    def test_analysis_wins_even_when_jev_says_media(self):
        # Reply-analysis precedence runs before JEV is even consulted.
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=_jev(route="media", confidence=0.99)),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "این عکس را از نظر امنیتی بررسی کن؛ فقط تحلیل کن.",
                chat_id=-1,
                provider=_Boom(),
                has_reply=True,
            )
        self.assertFalse(ok)


class AdminVerdictThenPermissionDeniedTests(TestCase):
    """Security Gate stays authoritative after a confident admin verdict."""

    def setUp(self):
        clear_role_cache()
        classify_cache.clear()
        self.chat_id = -1001
        GroupSettings.objects.create(chat_id=self.chat_id, chat_title="G")

    def test_admin_routed_but_non_admin_denied(self):
        from aiogram.enums import ChatMemberStatus as _S
        from botapp.agent.orchestrator import handle_admin_command
        from .test_agent import FakeBot

        bot = FakeBot(members={42: _member(_S.MEMBER)})
        msg = make_message("تعداد اعضای گروه چقدره؟", user_id=42)
        result = async_to_sync(handle_admin_command)(
            bot, msg, "تعداد اعضای گروه چقدره؟"
        )
        self.assertTrue(result.error)
        self.assertIn("مدیران", result.text)

    def test_admin_routed_member_count_executes_read(self):
        from botapp.agent.orchestrator import handle_admin_command

        bot = admin_bot(42)
        msg = make_message("x", user_id=42)
        result = async_to_sync(handle_admin_command)(
            bot, msg, "تعداد اعضای گروه چقدره؟"
        )
        self.assertFalse(result.error, result.text)
        self.assertFalse(result.needs_confirmation)
        self.assertEqual(bot.sent, [])

    def test_admin_routed_ban_keeps_confirmation_and_protected_target(self):
        from botapp.agent.orchestrator import handle_admin_command

        bot = admin_bot(42, target_id=7)
        msg = make_message("x", user_id=42, reply_user_id=7, reply_message_id=4)
        result = async_to_sync(handle_admin_command)(
            bot, msg, "این کاربر رو بن کن"
        )
        self.assertTrue(result.needs_confirmation)
        self.assertFalse(bot.banned)


class HarnessVerdictRunsBridgeTests(TestCase):
    """A harness verdict runs the real planner→validator→security→executor chain."""

    def setUp(self):
        clear_role_cache()
        classify_cache.clear()
        GroupSettings.objects.get_or_create(
            chat_id=-1001, defaults={"chat_title": "Harness Group"}
        )

    @override_settings(
        AGENT_ENABLED=True,
        AGENT_AI_ENABLED=False,
        AGENT_HARNESS_ENABLED=True,
        AGENT_HARNESS_AI_ENABLED=False,
    )
    def test_harness_route_executes_bridge_and_stores_run(self):
        from botapp.agent.orchestrator import handle_admin_command
        from botapp.harness.bridge import (
            get_bridge_store,
            get_last_bridge_result,
            reset_bridge_store,
        )

        reset_bridge_store()
        bot = admin_bot(42)
        result = async_to_sync(handle_admin_command)(
            bot,
            make_message("x", chat_id=-1001, user_id=42),
            "بررسی کامل کن",
        )
        self.assertFalse(result.error, result.text)
        bridged = get_last_bridge_result()
        self.assertIsNotNone(bridged)
        self.assertTrue(bridged.run_id)
        self.assertTrue(bridged.observer_verdict)
        stored = get_bridge_store().get(bridged.run_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.run_id, bridged.run_id)


class JevDownFallbackTests(SimpleTestCase):
    def setUp(self):
        classify_cache.clear()
        jev_router._cool_until = 0.0

    def test_jev_down_structured_provider_decides(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=None),
        ):
            provider = _Structured(route="admin", confidence=0.9,
                                   intent="admin_command")
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42, provider=provider
            )
        self.assertTrue(ok)

    def test_jev_down_legacy_provider_decides(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=None),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه کار عجیب مدیریتی بکن", chat_id=-42,
                provider=_LegacyLlm("agent", 0.95),
            )
        self.assertTrue(ok)

    def test_jev_down_deterministic_validates_last_resort(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=None),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "تعداد اعضای گروه چقدره؟", chat_id=-42, provider=None
            )
        self.assertTrue(ok)

    def test_jev_down_unknown_chat_stays_in_chat(self):
        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(return_value=None),
        ):
            ok = async_to_sync(should_route_to_agent)(
                "یه حرف نامفهوم بی‌ربط ززز", chat_id=-42, provider=None
            )
        self.assertFalse(ok)

    def test_jev_empty_and_timeout_fall_back_safely(self):
        async def _both_none(*a, **k):
            return None

        with patch(
            "botapp.agent.jev_router.jev_classify_request",
            new=AsyncMock(side_effect=_both_none),
        ):
            verdict = async_to_sync(classify_request)(
                "", chat_id=-1, provider=None, use_jev=True
            )
        assert verdict is not None
        self.assertEqual(verdict.route, "chat")
