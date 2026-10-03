"""Agent C: harness security gate tests (botapp/harness/security.py).

Covers: read ALLOW, write NEED_CONFIRMATION, fail-closed DENY paths,
reply-truth over planner ids, risk-upward, channel guard, protected
targets, harness C7 allowlist predicate (structured metadata only),
self-call refusal, execute-time revalidation (fresh identity, no cache).
"""
from types import SimpleNamespace

from aiogram.enums import ChatMemberStatus
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

import botapp.agent_tools  # noqa: F401  (registers admin tools)
from botapp.agent.context import AgentContext, ReplyTarget
from botapp.agent.permissions import (
    ADMINISTRATOR,
    MODERATOR,
    AdminIdentity,
    BotCapabilities,
    clear_role_cache,
)
from botapp.harness.security import (
    AuthVerdict,
    authorize,
    authorize_harness_step,
    build_harness_allowlist,
    is_harness_executable,
    revalidate_for_execution,
)
from botapp.agent.registry import registry


def _member(status, **caps):
    base = {
        "status": status,
        "can_restrict_members": False,
        "can_delete_messages": False,
        "can_pin_messages": False,
        "can_promote_members": False,
        "can_change_info": False,
    }
    base.update(caps)
    user = SimpleNamespace(id=base.pop("user_id", 0), full_name="", username="", is_bot=False)
    return SimpleNamespace(user=user, **base)


class _Bot:
    def __init__(self, members):
        self._members = members

    async def me(self):
        return SimpleNamespace(id=1000, username="b", full_name="B")

    async def get_chat_member(self, chat_id, user_id):
        return self._members.get(user_id, _member(ChatMemberStatus.MEMBER))


def _ctx(role=ADMINISTRATOR, reply=None, chat_type="supergroup", caps=True):
    return AgentContext(
        chat_id=-1001,
        chat_type=chat_type,
        chat_title="G",
        admin=AdminIdentity(user_id=42, role=role, display_name="A"),
        bot_capabilities=BotCapabilities(
            is_admin=caps,
            can_restrict_members=caps,
            can_delete_messages=caps,
            can_pin_messages=caps,
            can_post_messages=caps,
        ),
        reply=reply,
    )


def _reply(uid=7):
    return ReplyTarget(
        user_id=uid, is_bot=False, full_name="T", username="t", message_id=4, excerpt="x"
    )


def _bot_all():
    return _Bot(
        {
            1000: _member(ChatMemberStatus.ADMINISTRATOR, can_restrict_members=True,
                          can_delete_messages=True, can_pin_messages=True),
            42: _member(ChatMemberStatus.ADMINISTRATOR, can_promote_members=True,
                        can_restrict_members=True),
            7: _member(ChatMemberStatus.MEMBER),
        }
    )


class SecurityGateTests(SimpleTestCase):
    def setUp(self):
        clear_role_cache()

    def test_read_allow(self):
        d = async_to_sync(authorize)(_ctx(), "analytics.get_today_summary", {})
        self.assertEqual(d.verdict, AuthVerdict.ALLOW)
        self.assertEqual(d.risk, "low")

    def test_write_needs_confirmation(self):
        d = async_to_sync(authorize)(_ctx(reply=_reply()), "member.mute",
                                    {"duration_minutes": 5}, bot=_bot_all())
        self.assertEqual(d.verdict, AuthVerdict.NEED_CONFIRMATION)
        self.assertEqual(d.risk, "high")

    def test_reply_truth_over_planner_id(self):
        d = async_to_sync(authorize)(
            _ctx(reply=_reply()), "member.mute",
            {"target_user_id": 999999}, bot=_bot_all())
        self.assertEqual(d.validated_params["target_user_id"], 7)

    def test_risk_downgrade_ignored(self):
        d = async_to_sync(authorize)(_ctx(reply=_reply()), "member.ban", {},
                                    ai_risk="low", bot=_bot_all())
        self.assertEqual(d.risk, "high")
        self.assertEqual(d.verdict, AuthVerdict.NEED_CONFIRMATION)

    def test_channel_guard_deny(self):
        d = async_to_sync(authorize)(
            _ctx(reply=_reply(), chat_type="channel"), "member.mute", {}, bot=_bot_all())
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_non_admin_deny(self):
        d = async_to_sync(authorize)(_ctx(role="none"), "analytics.get_today_summary", {})
        self.assertEqual(d.verdict, AuthVerdict.DENY)
        self.assertEqual(d.reason_code, "permission_denied")

    def test_ambiguous_target_deny(self):
        d = async_to_sync(authorize)(_ctx(), "member.mute", {})
        self.assertEqual(d.verdict, AuthVerdict.DENY)
        self.assertEqual(d.reason_code, "ambiguous_target")

    def test_unknown_tool_deny(self):
        d = async_to_sync(authorize)(_ctx(), "shell.exec", {})
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_protected_target_deny(self):
        bot = _Bot({
            1000: _member(ChatMemberStatus.ADMINISTRATOR),
            7: _member(ChatMemberStatus.ADMINISTRATOR,
                       user_id=7, can_restrict_members=True),
        })
        d = async_to_sync(authorize)(_ctx(reply=_reply()), "member.ban", {}, bot=bot)
        self.assertEqual(d.verdict, AuthVerdict.DENY)
        self.assertEqual(d.reason_code, "protected_target")

    def test_harness_allowlist_read_only(self):
        names = build_harness_allowlist()
        for n in names:
            t = registry.get(n)
            self.assertEqual(t.risk_level, "low")
            self.assertFalse(t.requires_confirmation)
            self.assertNotEqual(n, "harness.investigate")
        self.assertNotIn("member.mute", names)
        self.assertNotIn("harness.investigate", names)

    def test_harness_predicate_no_prefix_heuristics(self):
        for t in registry.all():
            expect = (
                t.category == "admin" and t.risk_level == "low"
                and not t.requires_confirmation and t.name != "harness.investigate"
            )
            self.assertEqual(is_harness_executable(t), expect, t.name)

    def test_harness_step_write_denied(self):
        al = build_harness_allowlist()
        d = async_to_sync(authorize_harness_step)(_ctx(MODERATOR), "member.mute", allowlist=al)
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_harness_step_self_call_denied(self):
        d = async_to_sync(authorize_harness_step)(
            _ctx(MODERATOR), "harness.investigate", allowlist=build_harness_allowlist())
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_harness_step_read_allow(self):
        al = build_harness_allowlist()
        d = async_to_sync(authorize_harness_step)(
            _ctx(MODERATOR), "analytics.get_today_summary", allowlist=al)
        self.assertEqual(d.verdict, AuthVerdict.ALLOW)

    def test_execute_time_demoted_denied(self):
        bot = _Bot({
            1000: _member(ChatMemberStatus.ADMINISTRATOR, can_restrict_members=True),
            42: _member(ChatMemberStatus.MEMBER),
            7: _member(ChatMemberStatus.MEMBER),
        })
        d = async_to_sync(revalidate_for_execution)(
            bot, chat_id=-1001, requester_user_id=42,
            tool_name="member.mute",
            stored_params={"target_user_id": 7, "duration_minutes": 5},
            ctx=_ctx())
        self.assertEqual(d.verdict, AuthVerdict.DENY)
        self.assertEqual(d.reason_code, "permission_denied")

    def test_execute_time_capability_denied(self):
        bot = _Bot({
            1000: _member(ChatMemberStatus.MEMBER),
            42: _member(ChatMemberStatus.ADMINISTRATOR, can_promote_members=True),
            7: _member(ChatMemberStatus.MEMBER),
        })
        d = async_to_sync(revalidate_for_execution)(
            bot, chat_id=-1001, requester_user_id=42,
            tool_name="member.mute",
            stored_params={"target_user_id": 7, "duration_minutes": 5},
            ctx=_ctx())
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_execute_time_removed_tool_denied(self):
        d = async_to_sync(revalidate_for_execution)(
            _bot_all(), chat_id=-1001, requester_user_id=42,
            tool_name="member.__removed__", stored_params={}, ctx=_ctx())
        self.assertEqual(d.verdict, AuthVerdict.DENY)
