"""Focused tests for Agent B harness planning domain. Pure stdlib+unittest."""

import unittest

from botapp.harness import (
    AIPlannerAdapter,
    PlanRejected,
    deterministic_plan,
    deterministic_plan_steps,
    resolve_max_steps,
    to_harness_step,
    topological_order,
    validate_dag,
    validate_plan,
    validate_raw_step,
)


def _tool_step(sid="s01", tool="analytics.get_today_summary", deps=None):
    return {
        "id": sid,
        "depends_on": deps or [],
        "action": "call_tool",
        "tool": tool,
        "args": {},
        "thinking": "t",
    }


def _plan_dict(steps, max_steps=12):
    return {"kind": "harness.plan/v1", "budget": {"max_steps": max_steps}, "steps": steps}


class TaxonomyTests(unittest.TestCase):
    def test_unknown_code_raises(self):
        with self.assertRaises(ValueError):
            PlanRejected(code="nope")

    def test_known_code_message(self):
        err = PlanRejected(code="denied_tool", step_id="s01")
        self.assertEqual(err.code, "denied_tool")
        self.assertEqual(err.step_id, "s01")
        self.assertTrue(err.user_message)


class SchemasTests(unittest.TestCase):
    def test_target_source_frozen(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_raw_step({**_tool_step(), "target_source": "user_id"})
        self.assertEqual(cm.exception.code, "invalid_schema")

    def test_resolve_max_steps(self):
        self.assertEqual(resolve_max_steps(None), 5)
        self.assertEqual(resolve_max_steps(8), 8)
        with self.assertRaises(PlanRejected):
            resolve_max_steps(0)
        with self.assertRaises(PlanRejected):
            resolve_max_steps("junk")

    def test_to_harness_step_forces_empty_args(self):
        step = validate_raw_step(_tool_step())
        out = to_harness_step(step)
        self.assertEqual(out["args"], {})


class DagTests(unittest.TestCase):
    def test_topological_order_deterministic(self):
        a = validate_raw_step(_tool_step("b", deps=["a"]))
        b = validate_raw_step(_tool_step("a"))
        c = validate_raw_step(_tool_step("c", deps=["a", "b"]))
        order = topological_order([c, a, b])
        self.assertEqual([s.id for s in order], ["a", "b", "c"])

    def test_duplicate(self):
        steps = [validate_raw_step(_tool_step("a")), validate_raw_step(_tool_step("a"))]
        with self.assertRaises(PlanRejected) as cm:
            validate_dag(steps)
        self.assertEqual(cm.exception.code, "duplicate_step_id")

    def test_unknown_dep(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_dag([validate_raw_step(_tool_step("a", deps=["zzz"]))])
        self.assertEqual(cm.exception.code, "unknown_dependency")

    def test_self_dep(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_dag([validate_raw_step(_tool_step("a", deps=["a"]))])
        self.assertEqual(cm.exception.code, "self_dependency")

    def test_cycle(self):
        steps = [
            validate_raw_step(_tool_step("a", deps=["b"])),
            validate_raw_step(_tool_step("b", deps=["a"])),
        ]
        with self.assertRaises(PlanRejected) as cm:
            validate_dag(steps)
        self.assertEqual(cm.exception.code, "cycle_detected")

    def test_multiple_finish(self):
        f = {"id": "f1", "action": "finish", "answer": "x", "thinking": "t"}
        g = {"id": "f2", "action": "finish", "answer": "y", "thinking": "t"}
        steps = [validate_raw_step(f), validate_raw_step(g)]
        with self.assertRaises(PlanRejected) as cm:
            validate_dag(steps)
        self.assertEqual(cm.exception.code, "multiple_finish")

    def test_finish_not_terminal(self):
        f = {"id": "f1", "action": "finish", "answer": "x", "thinking": "t"}
        steps = [validate_raw_step(f), validate_raw_step(_tool_step("s2", deps=["f1"]))]
        with self.assertRaises(PlanRejected) as cm:
            validate_dag(steps)
        self.assertEqual(cm.exception.code, "finish_not_terminal")


class ValidationTests(unittest.TestCase):
    def test_unknown_action(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_raw_step({"action": "dance"})
        self.assertEqual(cm.exception.code, "unknown_action")

    def test_self_tool_denied(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(_plan_dict([_tool_step(tool="harness.investigate")]))
        self.assertEqual(cm.exception.code, "denied_tool")

    def test_allowlist_deny(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(
                _plan_dict([_tool_step()]),
                allowlist=["other.tool"],
            )
        # allowed tool passes
        res = validate_plan(
            _plan_dict([_tool_step()]),
            allowlist=["analytics.get_today_summary"],
        )
        self.assertEqual(len(res.steps), 1)
        self.assertEqual(cm.exception.code, "denied_tool")

    def test_args_must_be_empty(self):
        bad = _tool_step()
        bad["args"] = {"x": 1}
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(_plan_dict([bad]))
        self.assertEqual(cm.exception.code, "invalid_params")

    def test_empty_code(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_raw_step({"id": "c", "action": "run_code", "code": "  ", "thinking": "t"})
        self.assertEqual(cm.exception.code, "empty_code")

    def test_code_contract(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_raw_step({"id": "c", "action": "run_code", "code": "x = 1", "thinking": "t"})
        self.assertEqual(cm.exception.code, "code_contract")

    def test_code_ok(self):
        step = validate_raw_step(
            {"id": "c", "action": "run_code", "code": "result = data.get('count', 0)", "thinking": "t"}
        )
        self.assertEqual(step.action, "run_code")

    def test_finish_empty_rejects(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_raw_step({"id": "f", "action": "finish", "answer": " ", "thinking": "t"})
        self.assertEqual(cm.exception.code, "empty_answer")

    def test_thinking_truncates_not_rejects(self):
        step = validate_raw_step({**_tool_step(), "thinking": "x" * 500})
        self.assertEqual(len(step.thinking), 400)

    def test_bad_kind(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_plan({"kind": "other/v9", "budget": {"max_steps": 8}, "steps": [_tool_step()]})
        self.assertEqual(cm.exception.code, "bad_kind")

    def test_empty_plan(self):
        with self.assertRaises(PlanRejected) as cm:
            validate_plan({"kind": "harness.plan/v1", "budget": {"max_steps": 8}, "steps": []})
        self.assertEqual(cm.exception.code, "empty_plan")

    def test_budget_cap(self):
        steps = [_tool_step(f"s{i:02d}") for i in range(3)]
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(_plan_dict(steps, max_steps=2))
        self.assertEqual(cm.exception.code, "max_steps_exceeded")

    def test_deterministic_fallback_shape(self):
        from botapp.harness import executable_step_count
        from botapp.harness.limits import MAX_STEPS
        from botapp.harness.schemas import DETERMINISTIC_TOOLS

        steps = deterministic_plan_steps()
        self.assertEqual(steps[-1].action, "finish")
        self.assertEqual(steps[-1].answer, "")
        res = deterministic_plan()
        self.assertEqual(res.error, "deterministic")
        # Full frozen §3 chain, untruncated: 7 tools + code + finish marker,
        # i.e. exactly 8 executable steps; budget counts executable nodes.
        self.assertEqual(len(DETERMINISTIC_TOOLS), 7)
        self.assertEqual(
            [s.action for s in steps],
            ["call_tool"] * 7 + ["run_code", "finish"],
        )
        self.assertEqual(
            [s.tool for s in steps if s.action == "call_tool"],
            list(DETERMINISTIC_TOOLS),
        )
        self.assertEqual(len(res.plan.steps), 9)
        self.assertEqual(executable_step_count(res.plan.steps), 8)
        self.assertEqual(res.plan.budget.max_steps, 8)
        self.assertEqual(MAX_STEPS, 8)

    def test_deterministic_full_chain_preserved_regardless_of_cap(self):
        from botapp.harness import executable_step_count
        from botapp.harness.limits import MAX_STEPS
        from botapp.harness.schemas import DETERMINISTIC_TOOLS

        # No required tool is ever truncated: every cap returns all 7 tools
        # in frozen order + code pass + finish marker.
        for cap in (None, 5, 8, 12, 200):
            kw = {} if cap is None else {"max_steps": cap}
            res = deterministic_plan(**kw)
            self.assertEqual(
                [s.tool for s in res.plan.steps if s.action == "call_tool"],
                list(DETERMINISTIC_TOOLS),
            )
            self.assertEqual(executable_step_count(res.plan.steps), 8)
            self.assertEqual(res.plan.budget.max_steps, 8)
            self.assertEqual(MAX_STEPS, 8)

    def test_finish_marker_is_terminal_only_not_execution_slot(self):
        from botapp.harness import executable_step_count

        steps = deterministic_plan_steps()
        finishes = [s for s in steps if s.action == "finish"]
        self.assertEqual(len(finishes), 1)
        # Finish carries no tool/capability and no executable work.
        self.assertEqual(finishes[0].tool, "")
        self.assertEqual(finishes[0].code, "")
        self.assertEqual(
            executable_step_count(steps),
            len([s for s in steps if s.action in ("call_tool", "run_code")]),
        )

    def test_ninth_executable_tool_step_rejected(self):
        from botapp.harness.limits import MAX_STEPS

        # 7 executable tools + code + finish marker = 9 nodes / 8 executable:
        # fits at cap 8. A 9th *executable* tool step must be rejected even
        # when the declared budget is larger.
        tools8 = [
            "analytics.get_message_activity_today",
            "analytics.get_top_senders_today",
            "analytics.get_today_summary",
            "group.get_member_count",
            "group.get_moderation_status",
            "group.get_schedules",
            "audit.get_recent_actions",
            "group.get_admins",
        ]
        self.assertEqual(MAX_STEPS, 8)
        steps = [
            {
                "id": f"s{i + 1:02d}",
                "depends_on": [f"s{i:02d}"] if i else [],
                "action": "call_tool",
                "tool": t,
                "args": {},
                "thinking": "t",
            }
            for i, t in enumerate(tools8)
        ]
        # 9th executable BEFORE the terminal finish marker: 9 executable
        # nodes + finish = 10 nodes, executable budget (8) must fire.
        steps.append(
            {
                "id": "s09",
                "depends_on": ["s08"],
                "action": "call_tool",
                "tool": "group.get_settings",
                "args": {},
                "thinking": "t",
            }
        )
        steps.append(
            {
                "id": "s10",
                "depends_on": ["s09"],
                "action": "finish",
                "answer": "پایان.",
                "thinking": "t",
            }
        )
        with self.assertRaises(PlanRejected) as cm:
            validate_plan(
                _plan_dict(steps, max_steps=12),
                allowlist=[s["tool"] for s in steps if s["action"] == "call_tool"],
            )
        self.assertEqual(cm.exception.code, "max_steps_exceeded")

    def test_adapter(self):
        adapter = AIPlannerAdapter()
        step = adapter.parse_step(_tool_step())
        self.assertEqual(step.tool, "analytics.get_today_summary")
        self.assertEqual(adapter.max_steps_or_default(None), 5)


if __name__ == "__main__":
    unittest.main()
