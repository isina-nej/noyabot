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
        steps = deterministic_plan_steps()
        self.assertEqual(len(steps), 9)
        self.assertEqual(steps[-1].action, "finish")
        self.assertEqual(steps[-1].answer, "")
        res = deterministic_plan()
        self.assertEqual(res.error, "deterministic")
        self.assertEqual(len(res.plan.steps), 9)

    def test_adapter(self):
        adapter = AIPlannerAdapter()
        step = adapter.parse_step(_tool_step())
        self.assertEqual(step.tool, "analytics.get_today_summary")
        self.assertEqual(adapter.max_steps_or_default(None), 5)


if __name__ == "__main__":
    unittest.main()
