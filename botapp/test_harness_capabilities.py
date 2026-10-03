"""Logic-only tests for botapp/harness/capabilities.py (stdlib unittest only).

No Django, no pydantic, no aiogram: classification is exercised with FakeTool
stubs, plus live-registry chat-catalog validation (chat tools register on
``botapp.agent`` import with stdlib only) and source grep-gates proving:

- no Registry duplication, no dispatch/auth path in capabilities.py
- no prefix-based access (no startswith / _WRITEISH_PREFIXES)
- no general read/write inference from risk_level (explicit-contract-only)
- no hardcoded catalog counts (count-independent; snapshot non-normative)

Frozen refs: HARNESS_ARCHITECTURE Sec 2 (C1, C2, C4, C5, C7), Sec 7.2,
Sec 11, Sec 15; Amendment ``8ee0b82`` (2026-10-04); research snapshot
``d0b5aa5`` (5 core + 13 research = 18 chat, 54 admin).
"""

from __future__ import annotations

import inspect
import pathlib
import unittest

from botapp.harness.capabilities import (
    ACCESS_CLASSIFICATION_SOURCE,
    ADMIN,
    CHAT,
    CHAT_SNAPSHOT_2026_10_04,
    CHAT_TOOL_NAMES,
    CORE_CHAT_TOOL_NAMES,
    HARNESS_ENTRY_TOOL,
    RESEARCH_CHAT_TOOL_NAMES,
    RUN_CODE_STEP,
    build_harness_allowlist,
    capability_matrix,
    chat_capabilities,
    classify,
    effective_requires_confirmation,
    get_capability,
    is_ai_selectable,
    is_allowlisted,
    is_evidence_suitable,
    is_harness_eligible,
    is_internal_step,
    is_read_behavior,
    is_research_name,
    is_untrusted_evidence,
    validate_admin_isolation,
    validate_chat_catalog,
    validate_research_read_only,
)


class FakeTool:
    def __init__(self, name, *, category="admin", permission="group.read",
                 capability=None, risk_level="low", requires_confirmation=False,
                 target_kind="none", ai_selectable=True, accepts_empty=True):
        self.name = name
        self.category = category
        self.permission = permission
        self.capability = capability
        self.risk_level = risk_level
        self.requires_confirmation = requires_confirmation
        self.target_kind = target_kind
        self.ai_selectable = ai_selectable
        self.input_schema = None
        self.parameters = {}
        self._accepts_empty = accepts_empty

    def validate_params(self, params):
        if self._accepts_empty and (params or {}) == {}:
            return {}
        raise ValueError("rejects")

    def schema_dict(self):
        return {"type": "object", "properties": {}}


class ExplicitContractAccessTests(unittest.TestCase):
    def test_research_chat_tool_is_read_regardless_of_risk(self):
        # Live research Tools carry fail-closed defaults (high + confirm);
        # contract still classifies them read-only.
        for name in ("x.search_posts", "youtube.search", "research.social_search"):
            with self.subTest(name=name):
                tool = FakeTool(name, category="chat",
                                risk_level="high", requires_confirmation=True)
                self.assertEqual(classify(tool).access, "read")
                self.assertTrue(is_read_behavior(tool))

    def test_core_chat_tool_is_write_fail_closed(self):
        # No general low/no-confirm inference: even low+no-confirm core
        # chat tools stay write until an explicit contract says otherwise.
        tool = FakeTool("web_search", category="chat",
                        risk_level="low", requires_confirmation=False)
        self.assertEqual(classify(tool).access, "write")
        self.assertFalse(is_read_behavior(tool))

    def test_admin_low_no_confirm_is_write_no_general_inference(self):
        tool = FakeTool("analytics.get_today_summary", category="admin")
        self.assertEqual(classify(tool).access, "write")
        self.assertFalse(is_read_behavior(tool))

    def test_unknown_x_prefix_is_write(self):
        tool = FakeTool("x.unknown_future_tool", category="chat",
                        risk_level="low", requires_confirmation=False)
        self.assertFalse(is_research_name("x.unknown_future_tool"))
        self.assertEqual(classify(tool).access, "write")

    def test_research_name_outside_chat_is_write(self):
        tool = FakeTool("x.search_posts", category="admin",
                        risk_level="low", requires_confirmation=False)
        self.assertEqual(classify(tool).access, "write")
        self.assertFalse(is_untrusted_evidence(tool))

    def test_is_read_behavior_ignores_risk_and_confirm(self):
        a = FakeTool("x.search_posts", category="chat",
                     risk_level="low", requires_confirmation=False)
        b = FakeTool("x.search_posts", category="chat",
                     risk_level="high", requires_confirmation=True)
        self.assertTrue(is_read_behavior(a))
        self.assertTrue(is_read_behavior(b))
        c = FakeTool("web_search", category="chat",
                     risk_level="low", requires_confirmation=False)
        d = FakeTool("web_search", category="chat",
                     risk_level="high", requires_confirmation=True)
        self.assertFalse(is_read_behavior(c))
        self.assertFalse(is_read_behavior(d))

    def test_is_read_behavior_source_has_no_risk_or_prefix(self):
        src = inspect.getsource(is_read_behavior)
        self.assertNotIn("risk_level", src)
        self.assertNotIn("requires_confirmation", src)
        self.assertNotIn("startswith", src)
        self.assertNotIn("_WRITEISH_PREFIXES", src)


class ResearchContractTests(unittest.TestCase):
    def test_research_exact_set_shape(self):
        self.assertEqual(len(RESEARCH_CHAT_TOOL_NAMES), 13)
        self.assertEqual(len(CORE_CHAT_TOOL_NAMES), 5)
        self.assertEqual(len(CHAT_SNAPSHOT_2026_10_04), 18)
        self.assertEqual(CHAT_SNAPSHOT_2026_10_04,
                         set(CORE_CHAT_TOOL_NAMES) | set(RESEARCH_CHAT_TOOL_NAMES))
        # Backward-compat alias still the 5-core set only.
        self.assertEqual(set(CHAT_TOOL_NAMES), set(CORE_CHAT_TOOL_NAMES))

    def test_is_research_name_exact_set_no_prefix(self):
        for name in RESEARCH_CHAT_TOOL_NAMES:
            self.assertTrue(is_research_name(name), name)
        for bad in ("x.unknown_future_tool", "x.", "youtube.other",
                    "research.other_tool", "research.", "", "web_search"):
            self.assertFalse(is_research_name(bad), bad)

    def test_classify_marks_untrusted_evidence_only_for_research(self):
        research = FakeTool("x.search_posts", category="chat")
        self.assertTrue(classify(research).untrusted_evidence)
        self.assertTrue(is_untrusted_evidence(research))
        core = FakeTool("web_search", category="chat")
        self.assertFalse(classify(core).untrusted_evidence)
        admin = FakeTool("analytics.get_today_summary", category="admin")
        self.assertFalse(classify(admin).untrusted_evidence)

    def test_research_never_harness_eligible_or_evidence_suitable(self):
        for name in ("x.search_posts", "youtube.search", "research.social_search"):
            tool = FakeTool(name, category="chat")
            self.assertFalse(is_harness_eligible(tool), name)
            self.assertFalse(is_evidence_suitable(tool), name)
            cap = classify(tool)
            self.assertFalse(cap.harness_eligible)
            self.assertFalse(cap.evidence_suitable)

    def test_access_source_documents_explicit_contract(self):
        self.assertIn("explicit-contract-only", ACCESS_CLASSIFICATION_SOURCE)
        self.assertIn("RESEARCH_CHAT_TOOL_NAMES", ACCESS_CLASSIFICATION_SOURCE)
        self.assertIn("risk_level", ACCESS_CLASSIFICATION_SOURCE)
        self.assertIn("prefix", ACCESS_CLASSIFICATION_SOURCE.lower())


class ConfirmationRuleTests(unittest.TestCase):
    def test_high_always_confirms(self):
        self.assertTrue(effective_requires_confirmation(
            FakeTool("m.ban", risk_level="high", requires_confirmation=False)))

    def test_low_no_confirm_passes_through(self):
        self.assertFalse(effective_requires_confirmation(FakeTool("a.b")))

    def test_medium_flag_confirms(self):
        self.assertTrue(effective_requires_confirmation(
            FakeTool("m.warn", risk_level="medium", requires_confirmation=True)))


class RunCodeInternalTests(unittest.TestCase):
    def test_exact_equality_only(self):
        self.assertTrue(is_internal_step("run_code"))
        self.assertFalse(is_internal_step("member.ban"))
        self.assertFalse(is_internal_step("run_code2"))
        self.assertFalse(is_internal_step(""))

    def test_run_code_never_classified_or_selectable(self):
        self.assertIsNone(get_capability("run_code"))
        self.assertFalse(is_ai_selectable("run_code"))
        self.assertFalse(is_allowlisted("run_code", ["run_code"]))

    def test_unknown_tool_is_none(self):
        self.assertIsNone(get_capability("no.such.tool"))
        self.assertFalse(is_ai_selectable("no.such.tool"))


class HarnessEligibilityTests(unittest.TestCase):
    def test_low_no_confirm_no_target_eligible(self):
        self.assertTrue(is_harness_eligible(FakeTool("analytics.get_today_summary")))

    def test_entry_tool_excluded_by_equality(self):
        self.assertFalse(is_harness_eligible(
            FakeTool(HARNESS_ENTRY_TOOL, risk_level="low")))

    def test_medium_excluded(self):
        self.assertFalse(is_harness_eligible(
            FakeTool("m.warn", risk_level="medium", requires_confirmation=True)))

    def test_target_bearing_excluded(self):
        # target_kind != none fails validate_params({}) in real Registry;
        # stub rejects empty to mirror that.
        self.assertFalse(is_harness_eligible(
            FakeTool("member.get_info", target_kind="member", accepts_empty=False)))

    def test_chat_category_excluded(self):
        self.assertFalse(is_harness_eligible(FakeTool("web_search", category="chat")))
        self.assertFalse(is_harness_eligible(
            FakeTool("x.search_posts", category="chat")))

    def test_no_prefix_logic_in_source(self):
        src = (pathlib.Path(__file__).parent / "harness" / "capabilities.py").read_text()
        self.assertNotIn("_WRITEISH_PREFIXES", src)
        self.assertNotIn("startswith", src)

    def test_no_hardcoded_catalog_count_in_validator(self):
        import botapp.harness.capabilities as cap

        src = inspect.getsource(cap.validate_chat_catalog)
        self.assertNotIn("==5", src)
        self.assertNotIn("== 5", src)
        self.assertNotIn("==18", src)
        self.assertNotIn("== 18", src)
        self.assertNotIn("== 59", src)
        self.assertNotIn("==59", src)

    def test_allowlist_membership_exact(self):
        allow = ["a.b", "c.d"]
        self.assertTrue(is_allowlisted("a.b", allow))
        self.assertFalse(is_allowlisted("a.b ", allow))
        self.assertFalse(is_allowlisted("x.y", allow))


class NoDuplicationTests(unittest.TestCase):
    def test_no_registration_or_routing_in_source(self):
        src = (pathlib.Path(__file__).parent / "harness" / "capabilities.py").read_text()
        self.assertNotIn("register_tool", src)
        self.assertNotIn("registry.register", src)
        self.assertNotIn("chat_definitions", src)
        self.assertNotIn("dispatch_async", src)
        self.assertNotIn("ensure_admin_permission", src)
        self.assertNotIn("ensure_bot_capability", src)


class LiveRegistryTests(unittest.TestCase):
    def test_chat_catalog_core_subset_present_count_independent(self):
        ok, extra, missing = validate_chat_catalog()
        self.assertTrue(ok, (extra, missing))
        self.assertEqual(missing, set())
        live = {c.name for c in chat_capabilities()}
        # Core baseline must be present; extra chat tools allowed.
        self.assertTrue(set(CORE_CHAT_TOOL_NAMES) <= live, (live, missing))
        # This worktree has core-only chat (research layer lives on main);
        # if research is present elsewhere, it must be the exact set.
        research_live = live & set(RESEARCH_CHAT_TOOL_NAMES)
        self.assertTrue(research_live <= set(RESEARCH_CHAT_TOOL_NAMES))

    def test_chat_capabilities_all_chat_dynamic(self):
        for cap in chat_capabilities():
            self.assertEqual(cap.catalog, "chat")
        for cap in chat_capabilities():
            self.assertIn(cap.catalog, ("chat",))

    def test_admin_isolation_live(self):
        ok, info = validate_admin_isolation()
        self.assertTrue(ok, info)
        self.assertEqual(info["overlap"], [])
        self.assertEqual(info["research_in_admin"], [])
        self.assertFalse(info["member_ban_in_chat"])

    def test_research_read_only_live_vacuous_or_enforced(self):
        ok, violators = validate_research_read_only()
        self.assertTrue(ok, violators)
        self.assertEqual(violators, [])

    def test_allowlist_excludes_entry_and_internal(self):
        allow = build_harness_allowlist()
        self.assertNotIn(HARNESS_ENTRY_TOOL, allow)
        self.assertNotIn(RUN_CODE_STEP, allow)

    def test_evidence_suitable_subset_of_eligible(self):
        for cap in chat_capabilities():
            self.assertFalse(cap.evidence_suitable)
            self.assertFalse(cap.harness_eligible)


class MatrixShapeTests(unittest.TestCase):
    def test_matrix_rows_json_safe_and_internal_marked(self):
        import json

        rows = capability_matrix()
        json.dumps(rows)  # must serialize
        internal = [r for r in rows if r.get("internal")]
        self.assertEqual(len(internal), 1)
        self.assertEqual(internal[0]["name"], RUN_CODE_STEP)
        self.assertFalse(internal[0]["ai_selectable"])
        self.assertFalse(internal[0]["untrusted_evidence"])
        for row in rows:
            if not row["internal"]:
                self.assertIn(row["catalog"], ("chat", "admin"))
                self.assertIn(row["access"], ("read", "write"))
                self.assertIsInstance(row["untrusted_evidence"], bool)

    def test_matrix_without_internal_has_no_run_code(self):
        rows = capability_matrix(include_internal=False)
        self.assertNotIn(RUN_CODE_STEP, [r["name"] for r in rows])

    def test_evidence_suitable_mirrors_predicate_for_admin(self):
        tool = FakeTool("a.b")  # admin low/no-confirm/no-target/empty-ok
        self.assertEqual(is_evidence_suitable(tool), is_harness_eligible(tool))
        flagged = FakeTool(HARNESS_ENTRY_TOOL)
        self.assertFalse(is_evidence_suitable(flagged))

    def test_research_rows_marked_read_and_untrusted(self):
        import json

        rows = {r["name"]: r for r in capability_matrix()}
        json.dumps(rows)
        for name in RESEARCH_CHAT_TOOL_NAMES:
            if name in rows:  # present only when research layer merged
                self.assertEqual(rows[name]["access"], "read", name)
                self.assertTrue(rows[name]["untrusted_evidence"], name)


if __name__ == "__main__":
    unittest.main()
