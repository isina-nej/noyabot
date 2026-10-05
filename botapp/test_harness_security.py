"""Agent C: harness security gate tests (botapp/harness/security.py).

Covers: read ALLOW, write NEED_CONFIRMATION, fail-closed DENY paths,
reply-truth over planner ids, risk-upward (confirmation policy only),
channel guard, protected targets, harness C7/V6 eligibility predicate
(structured metadata only, no name prefixes), self-call/internal refusal,
dynamic chat/admin/research catalogs (no frozen name sets), research
read-only + UNTRUSTED evidence status (live registry where available,
code-design fallback otherwise), execute-time revalidation (fresh
identity, no cache). No Registry/shared-file edits.
"""
from types import SimpleNamespace

from aiogram.enums import ChatMemberStatus
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

import botapp.agent  # noqa: F401  (registers chat tools on import)
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
    ACCESS_CLASSIFICATION_SOURCE,
    HARNESS_ENTRY_TOOL,
    RUN_CODE_STEP,
    AuthVerdict,
    authorize,
    authorize_harness_step,
    admin_tool_names,
    build_harness_allowlist,
    chat_tool_names,
    is_admin_tool,
    is_chat_tool,
    is_harness_executable,
    is_internal_step,
    is_research_tool,
    research_catalog_status,
    research_tool_names,
    revalidate_for_execution,
)
from botapp.agent.registry import CHAT, registry


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


class FakeTool:
    def __init__(self, name, *, category="admin", risk_level="low",
                 requires_confirmation=False, target_kind="none", accepts_empty=True):
        self.name = name
        self.category = category
        self.risk_level = risk_level
        self.requires_confirmation = requires_confirmation
        self.target_kind = target_kind
        self._accepts_empty = accepts_empty
        self.handler = lambda: None
        self.handler.__module__ = "botapp.something"

    def validate_params(self, params):
        if self._accepts_empty and (params or {}) == {}:
            return SimpleNamespace(model_dump=lambda: {})
        raise ValueError("rejects")


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


class NoRiskFormulaOrPrefixTests(SimpleTestCase):
    def test_medium_no_confirm_is_write_capable_yet_not_harness_eligible(self):
        # settings.enable_* are medium + no-confirm: real writes (fail-closed
        # proof that risk/confirm is confirmation policy, not side effects).
        t = registry.get("settings.enable_anti_spam")
        self.assertEqual(t.risk_level, "medium")
        self.assertFalse(t.requires_confirmation)
        self.assertFalse(is_harness_executable(t))

    def test_chat_tools_carry_high_confirm_defaults(self):
        # Every registered chat tool carries high + confirm-required
        # defaults, so (low + no-confirm) can never mean "read-only".
        chat = [t for t in registry.all_tools() if t.category == CHAT]
        self.assertGreaterEqual(len(chat), 5)
        for t in chat:
            self.assertEqual((t.risk_level, t.requires_confirmation), ("high", True))

    def test_no_name_prefix_logic_in_security_source(self):
        import pathlib

        src = (pathlib.Path(__file__).parent / "harness" / "security.py").read_text()
        self.assertNotIn("_WRITEISH_PREFIXES", src)
        self.assertNotIn("startswith", src)

    def test_eligibility_predicate_is_structured_metadata_only(self):
        for t in registry.all():
            expect = (
                t.category == "admin" and t.risk_level == "low"
                and not t.requires_confirmation and t.name != HARNESS_ENTRY_TOOL
                and t.target_kind == "none"
            )
            # Live predicate adds the {} schema probe; FakeTool parity below.
            self.assertEqual(
                is_harness_executable(FakeTool(
                    t.name, category=t.category, risk_level=t.risk_level,
                    requires_confirmation=t.requires_confirmation,
                    target_kind=t.target_kind,
                    accepts_empty=(t.target_kind == "none" and t.risk_level == "low"
                                   and not t.requires_confirmation
                                   and t.name != HARNESS_ENTRY_TOOL))),
                expect, t.name)

    def test_target_bearing_low_no_confirm_not_eligible(self):
        stub = FakeTool("audit.get_actions_for_member", target_kind="member",
                        accepts_empty=False)
        self.assertFalse(is_harness_executable(stub))

    def test_internal_step_exact_equality(self):
        self.assertTrue(is_internal_step(RUN_CODE_STEP))
        self.assertFalse(is_internal_step("member.ban"))
        self.assertFalse(is_internal_step("run_code2"))
        self.assertFalse(is_internal_step(""))


class DynamicCatalogTests(SimpleTestCase):
    def test_chat_catalog_dynamic_no_frozen_set(self):
        import pathlib

        src = (pathlib.Path(__file__).parent / "harness" / "security.py").read_text()
        self.assertNotIn("CHAT_TOOL_NAMES", src)
        names = chat_tool_names()
        live = {t.name for t in registry.all_tools() if t.category == CHAT}
        self.assertEqual(set(names), live)
        self.assertGreaterEqual(len(names), 5)
        for base in ("web_search", "fetch_url", "get_time",
                     "generate_image", "text_to_speech"):
            self.assertIn(base, names)

    def test_admin_catalog_separate_from_chat(self):
        admin = set(admin_tool_names())
        chat = set(chat_tool_names())
        self.assertTrue(admin.isdisjoint(chat))
        self.assertIn("member.ban", admin)
        self.assertGreaterEqual(len(admin), 50)

    def test_research_subset_of_chat_no_prefix(self):
        import pathlib

        src = (pathlib.Path(__file__).parent / "harness" / "security.py").read_text()
        # No name-prefix family checks for research membership.
        for needle in ('name.startswith("x.")', "name.startswith('x.')",
                       'name.startswith("youtube")', "name.startswith('youtube')",
                       'name.startswith("research")', "name.startswith('research')"):
            self.assertNotIn(needle, src)
        names = research_tool_names()
        self.assertTrue(set(names) <= set(chat_tool_names()))

    def test_research_catalog_status_live(self):
        st = research_catalog_status()
        self.assertTrue(st["admin_catalog_separate"])
        self.assertEqual(st["results_trust"], "UNTRUSTED")
        self.assertTrue(st["evidence_capable"])
        self.assertEqual(st["count"], len(st["names"]))
        # Where the research package is installed, the full contract holds.
        try:
            import botapp.research  # noqa: F401
        except Exception:
            self.assertEqual(st["count"], 0)
            return
        self.assertEqual(st["count"], 13)
        self.assertIn("x.search_posts", st["names"])
        self.assertIn("youtube.search", st["names"])
        self.assertIn("research.social_search", st["names"])
        self.assertTrue(st["read_only"])
        self.assertIn("UNTRUSTED", st["evidence_source"])

    def test_research_results_untrusted_evidence_boundary(self):
        try:
            from botapp.research.evidence import format_research_evidence
            from botapp.research.models import ResearchItem
        except Exception:
            self.skipTest("botapp.research not installed in this worktree")
        out = format_research_evidence([ResearchItem(
            platform="x", source_type="post", source_id="1", author="a",
            title="t", text="hello", published_at=None, url="https://x.com/1")])
        self.assertIn("UNTRUSTED", out)
        self.assertIn("<external_source", out)


class HarnessStepTests(SimpleTestCase):
    def setUp(self):
        clear_role_cache()

    def test_harness_allowlist_excludes_writes_entry_internal(self):
        names = build_harness_allowlist()
        self.assertNotIn("member.mute", names)
        self.assertNotIn(HARNESS_ENTRY_TOOL, names)
        self.assertNotIn(RUN_CODE_STEP, names)
        for n in names:
            t = registry.get(n)
            self.assertEqual(t.risk_level, "low")
            self.assertFalse(t.requires_confirmation)

    def test_harness_step_write_denied(self):
        al = build_harness_allowlist()
        d = async_to_sync(authorize_harness_step)(_ctx(MODERATOR), "member.mute", allowlist=al)
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_harness_step_self_call_denied(self):
        d = async_to_sync(authorize_harness_step)(
            _ctx(MODERATOR), HARNESS_ENTRY_TOOL, allowlist=build_harness_allowlist())
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_harness_step_internal_denied(self):
        d = async_to_sync(authorize_harness_step)(
            _ctx(MODERATOR), RUN_CODE_STEP, allowlist=build_harness_allowlist())
        self.assertEqual(d.verdict, AuthVerdict.DENY)

    def test_harness_step_read_allow(self):
        al = build_harness_allowlist()
        d = async_to_sync(authorize_harness_step)(
            _ctx(MODERATOR), "analytics.get_today_summary", allowlist=al)
        self.assertEqual(d.verdict, AuthVerdict.ALLOW)


class ExecuteTimeTests(SimpleTestCase):
    def setUp(self):
        clear_role_cache()

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

    def test_access_source_documents_no_formula(self):
        self.assertIn("risk", ACCESS_CLASSIFICATION_SOURCE.lower())
        self.assertIn("prefix", ACCESS_CLASSIFICATION_SOURCE.lower())

    def test_chat_admin_live_checks(self):
        self.assertTrue(is_chat_tool("web_search"))
        self.assertFalse(is_chat_tool("member.ban"))
        self.assertFalse(is_chat_tool("no.such.tool"))
        self.assertFalse(is_chat_tool(RUN_CODE_STEP))
        self.assertTrue(is_admin_tool("member.ban"))
        self.assertFalse(is_admin_tool("web_search"))

    def test_is_research_tool_handler_module_not_prefix(self):
        live = {t.name for t in registry.all_tools() if is_research_tool(t)}
        self.assertEqual(live, set(research_tool_names()))
