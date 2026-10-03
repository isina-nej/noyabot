"""Logic-only tests for botapp/harness/capabilities.py (stdlib unittest only).

No Django, no pydantic, no aiogram: classification is exercised with FakeTool
stubs, plus live-registry chat-catalog validation (chat tools register on
``botapp.agent`` import with stdlib only) and a source grep-gate proving no
Registry duplication, no prefix-based auth, and no dispatch path.
"""

from __future__ import annotations

import pathlib
import unittest

from botapp.harness.capabilities import (
    CHAT_TOOL_NAMES,
    HARNESS_ENTRY_TOOL,
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
    validate_chat_catalog,
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


class ReadWriteAccessTests(unittest.TestCase):
    def test_low_no_confirm_is_read(self):
        self.assertEqual(classify(FakeTool("x.get_y")).access, "read")

    def test_medium_is_write(self):
        self.assertEqual(classify(FakeTool("m.warn", risk_level="medium",
                                           requires_confirmation=True)).access, "write")

    def test_high_no_flag_still_write(self):
        self.assertEqual(classify(FakeTool("m.ban", risk_level="high",
                                           requires_confirmation=False)).access, "write")

    def test_low_with_confirm_is_write(self):
        self.assertEqual(classify(FakeTool("s.set_x", requires_confirmation=True)).access,
                         "write")

    def test_is_read_behavior_predicate(self):
        self.assertTrue(is_read_behavior(FakeTool("a.b")))
        self.assertFalse(is_read_behavior(FakeTool("a.b", risk_level="high",
                                                   requires_confirmation=False)))


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

    def test_no_prefix_logic_in_source(self):
        src = (pathlib.Path(__file__).parent / "harness" / "capabilities.py").read_text()
        self.assertNotIn("_WRITEISH_PREFIXES", src)
        self.assertNotIn("startswith", src)

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
    def test_chat_catalog_is_exactly_five(self):
        ok, extra, missing = validate_chat_catalog()
        self.assertTrue(ok, (extra, missing))
        self.assertEqual(CHAT_TOOL_NAMES,
                         {"web_search", "fetch_url", "get_time", "generate_image",
                          "text_to_speech"})

    def test_chat_capabilities_all_chat(self):
        for cap in chat_capabilities():
            self.assertEqual(cap.catalog, "chat")

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
        for row in rows:
            if not row["internal"]:
                self.assertIn(row["catalog"], ("chat", "admin"))
                self.assertIn(row["access"], ("read", "write"))

    def test_matrix_without_internal_has_no_run_code(self):
        rows = capability_matrix(include_internal=False)
        self.assertNotIn(RUN_CODE_STEP, [r["name"] for r in rows])

    def test_evidence_suitable_mirrors_predicate(self):
        tool = FakeTool("a.b")
        self.assertEqual(is_evidence_suitable(tool), is_harness_eligible(tool))
        flagged = FakeTool(HARNESS_ENTRY_TOOL)
        self.assertFalse(is_evidence_suitable(flagged))


if __name__ == "__main__":
    unittest.main()
