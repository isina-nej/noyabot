"""Agent E: harness runtime tests (state/store, executor, observer).

Covers: sequential flow, tool budget, total-runtime budget, per-step
timeout, cancellation, partial failure, WAITING_CONFIRMATION + resume,
observer verdicts, MAX_REPLANS, structured refs, result size limits,
restart honesty. No shared-file imports beyond stdlib-adjacent harness.
"""

from __future__ import annotations

import asyncio
import time

from django.test import SimpleTestCase

from botapp.harness import (
    CONTINUE,
    FAIL,
    FINISH,
    MAX_REPLANS,
    MAX_RESULT_SIZE,
    MAX_STEPS,
    MAX_TOOL_CALLS,
    MAX_TOTAL_RUNTIME_S,
    REPLAN,
    WAIT,
    HarnessExecutor,
    HarnessRunState,
    InMemoryRunStore,
    Observer,
    ResultRef,
    RunNotFoundError,
    WAITING_CONFIRMATION,
    resolve_refs,
    truncate_result,
)


def _exec(mapping=None, record=None):
    mapping = mapping or {}

    def fn(tool, params):
        if record is not None:
            record.append((tool, dict(params)))
        if tool in mapping:
            v = mapping[tool]
            if isinstance(v, Exception):
                raise v
            return v() if callable(v) else v
        return f"ok:{tool}"
    return fn


class StateStoreTests(SimpleTestCase):
    def test_create_get_defensive_copy(self):
        store = InMemoryRunStore()
        ex = HarnessExecutor(store=store, tool_executor=_exec())
        run = ex.create_run([{"tool": "a"}])
        fetched = store.get(run.run_id)
        fetched.status = "tampered"
        self.assertNotEqual(store.get(run.run_id).status, "tampered")

    def test_recent_newest_first_capped(self):
        store = InMemoryRunStore(max_runs=200)
        ex = HarnessExecutor(store=store, tool_executor=_exec())
        ids = [ex.create_run([{"tool": "a"}]).run_id for _ in range(3)]
        recent = store.recent(limit=2)
        self.assertEqual([r.run_id for r in recent], [ids[2], ids[1]])
        self.assertEqual(len(store.recent(limit=500)), 3)  # capped at 50

    def test_eviction_oldest_first_full_runs(self):
        store = InMemoryRunStore(max_runs=2)
        ex = HarnessExecutor(store=store, tool_executor=_exec())
        r1 = ex.create_run([{"tool": "a"}]).run_id
        ex.create_run([{"tool": "b"}])
        ex.create_run([{"tool": "c"}])
        self.assertIsNone(store.get(r1))

    def test_restart_fails_honestly(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec())
        with self.assertRaises(RunNotFoundError):
            ex.resume("deadbeef" * 2)

    def test_result_size_limit(self):
        self.assertLessEqual(len(truncate_result("x" * (MAX_RESULT_SIZE + 100))), MAX_RESULT_SIZE + 30)
        d = truncate_result({"k": "y" * (MAX_RESULT_SIZE + 10)})
        self.assertLessEqual(len(d["k"]), MAX_RESULT_SIZE + 30)

    def test_structured_refs(self):
        self.assertEqual(resolve_refs({"v": {"$ref": "step:001"}}, {"001": "R"}), {"v": "R"})
        self.assertEqual(ResultRef(step_id="001").to_dict(), {"$ref": "step:001"})
        self.assertIsNone(ResultRef.from_dict({"nope": 1}))


class ExecutorTests(SimpleTestCase):
    def test_sequential_flow_and_result_passing(self):
        seen = []
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"s1": "AAA"}, seen))
        ex.tool_executor = lambda tool, params: (seen.append((tool, dict(params))) or ("AAA" if tool == "s1" else params.get("prev")))
        run = ex.create_run([{"tool": "s1"}, {"tool": "s2", "params": {"prev": {"$ref": "step:001"}}}])
        out = ex.run(run.run_id)
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(seen[1][1], {"prev": "AAA"})
        self.assertEqual(out.tool_calls_used, 2)

    def test_tool_call_budget(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec(), max_tool_calls=2)
        run = ex.create_run([{"tool": "a"}, {"tool": "b"}, {"tool": "c"}])
        out = ex.run(run.run_id)
        self.assertEqual(out.status, "failed")
        self.assertIn("tool_call", out.error)

    def test_total_runtime_budget(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec(), max_total_runtime_s=0)
        run = ex.create_run([{"tool": "a"}])
        out = ex.run(run.run_id)
        self.assertEqual(out.status, "failed")
        self.assertIn("runtime", out.error)
        self.assertEqual(MAX_TOTAL_RUNTIME_S, 90)

    def test_per_step_timeout(self):
        def slow(tool, params):
            if tool == "a":
                time.sleep(0.5)
                return "late"
            return "fast-ok"
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=slow, step_timeout_s=0.05)
        run = ex.create_run([{"tool": "a"}, {"tool": "b"}])
        out = ex.run(run.run_id)
        self.assertTrue(any(s.status == "failed" and "timeout" in s.error for s in out.steps))
        self.assertEqual(out.status, "succeeded")  # partial failure continues

    def test_partial_failure_records_and_continues(self):
        ex = HarnessExecutor(
            store=InMemoryRunStore(),
            tool_executor=_exec({"boom": RuntimeError("nope"), "fine": "OK"}),
        )
        run = ex.create_run([{"tool": "boom"}, {"tool": "fine"}])
        out = ex.run(run.run_id)
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(out.error, "partial_failure")
        self.assertEqual(out.steps[0].status, "failed")
        self.assertEqual(out.steps[1].status, "succeeded")

    def test_all_steps_fail_is_failed(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"a": RuntimeError("x")}))
        out = ex.run(ex.create_run([{"tool": "a"}]).run_id)
        self.assertEqual(out.status, "failed")

    def test_no_auto_retry_single_attempt(self):
        calls = []
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"a": RuntimeError("x")}, calls))
        ex.run(ex.create_run([{"tool": "a"}]).run_id)
        self.assertEqual(len(calls), 1)

    def test_cancellation_midrun(self):
        def canceller(tool, params):
            ex2.cancel(run_id_holder[0])
            return "x"
        ex2 = HarnessExecutor(store=InMemoryRunStore(), tool_executor=canceller)
        run_id_holder = [ex2.create_run([{"tool": "a"}, {"tool": "b"}]).run_id]
        out = ex2.run(run_id_holder[0])
        self.assertEqual(out.status, "cancelled")

    def test_cancel_terminal_is_noop(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"a": "R"}))
        run = ex.create_run([{"tool": "a"}])
        done = ex.run(run.run_id)
        again = ex.cancel(run.run_id)
        self.assertEqual(again.status, done.status)

    def test_max_steps_enforced(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec())
        with self.assertRaises(ValueError):
            ex.create_run([{"tool": f"t{i}"} for i in range(MAX_STEPS + 1)])
        self.assertEqual(MAX_STEPS, 8)

    def test_max_replans_enforced(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"a": "R"}), max_replans=MAX_REPLANS)
        run = ex.create_run([{"tool": "a"}])
        ex.run(run.run_id)
        with self.assertRaises(RuntimeError):
            ex.request_replan(run.run_id, [{"tool": "z"}])
        for _ in range(MAX_REPLANS):
            r = ex.create_run([{"tool": "a"}])
            ex.request_replan(r.run_id, [{"tool": "b"}])
        r = ex.create_run([{"tool": "a"}])
        for _ in range(MAX_REPLANS):
            ex.request_replan(r.run_id, [{"tool": "b"}])
        with self.assertRaises(Exception):
            ex.request_replan(r.run_id, [{"tool": "b"}])
        self.assertEqual(MAX_REPLANS, 2)
        self.assertEqual(MAX_TOOL_CALLS, 12)

    def test_async_run(self):
        async def go():
            async def aexec(tool, params):
                await asyncio.sleep(0)
                return f"r:{tool}"
            ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=aexec)
            return await ex.arun(ex.create_run([{"tool": "a"}, {"tool": "b"}]).run_id)
        out = asyncio.run(go())
        self.assertEqual(out.status, "succeeded")


class ConfirmationTests(SimpleTestCase):
    def test_wait_and_resume_with_approval(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"ok": "DONE"}))
        run = ex.create_run([{"tool": "gated", "requires_confirmation": True}, {"tool": "ok"}])
        paused = ex.run(run.run_id)
        self.assertEqual(paused.status, WAITING_CONFIRMATION)
        self.assertEqual(paused.steps[0].status, WAITING_CONFIRMATION)
        resumed = ex.resume(run.run_id, approvals={paused.steps[0].step_id: True})
        live = ex.store.get(run.run_id)
        self.assertIn(live.status, ("succeeded", "failed", WAITING_CONFIRMATION))
        # approved gate must have cleared
        self.assertNotEqual(live.steps[0].status, WAITING_CONFIRMATION)

    def test_wait_excluded_from_active_budget(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec())
        run = ex.create_run([{"tool": "g", "requires_confirmation": True}])
        paused = ex.run(run.run_id)
        self.assertEqual(paused.active_runtime_s, 0.0)
        self.assertNotEqual(paused.waiting_since, 0.0)

    def test_deny_skips_step(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec({"ok": "DONE"}))
        run = ex.create_run([{"tool": "g", "requires_confirmation": True}, {"tool": "ok"}])
        paused = ex.run(run.run_id)
        out = ex.resume(run.run_id, approvals={paused.steps[0].step_id: False})
        self.assertEqual(out.steps[0].status, "skipped")
        self.assertEqual(out.status, "succeeded")

    def test_resume_without_approval_stays_paused(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec())
        run = ex.create_run([{"tool": "g", "requires_confirmation": True}])
        paused = ex.run(run.run_id)
        again = ex.resume(run.run_id)
        self.assertEqual(again.status, WAITING_CONFIRMATION)


class ObserverTests(SimpleTestCase):
    def _run(self, **kw) -> HarnessRunState:
        base = {"run_id": "r1"}
        base.update(kw)
        return HarnessRunState(**base)

    def test_verdicts(self):
        ob = Observer()
        self.assertEqual(ob.observe(self._run(status="running", steps=[])).verdict, FINISH)
        r = self._run(status="running", steps=[])
        r.steps = []
        self.assertEqual(Observer().observe(self._run(status="cancelled")).verdict, FAIL)
        self.assertEqual(Observer().observe(self._run(status="succeeded")).verdict, FINISH)
        self.assertEqual(Observer().observe(self._run(status="failed", error="x")).verdict, FAIL)
        self.assertEqual(Observer().observe(self._run(status=WAITING_CONFIRMATION)).verdict, WAIT)

    def test_continue_replan_fail(self):
        from botapp.harness.state import HarnessStepState
        run = HarnessRunState(run_id="r", status="running", steps=[
            HarnessStepState(step_id="001", tool="a"),
        ])
        self.assertEqual(Observer().observe(run).verdict, CONTINUE)
        run.steps[0].status = "failed"
        run.current_index = 1
        rec = Observer().observe(run)
        self.assertEqual(rec.verdict, REPLAN)
        self.assertEqual(rec.replans_remaining, MAX_REPLANS)
        run.tool_calls_used = MAX_TOOL_CALLS
        self.assertEqual(Observer().observe(run).verdict, FAIL)
        run2 = HarnessRunState(run_id="r", status="running", steps=[
            HarnessStepState(step_id="001", tool="a", requires_confirmation=True),
        ])
        self.assertEqual(Observer().observe(run2).verdict, WAIT)

    def test_observer_is_read_only(self):
        from botapp.harness.state import HarnessStepState
        run = HarnessRunState(run_id="r", status="running", steps=[HarnessStepState(step_id="001", tool="a")])
        before = (run.status, run.current_index, len(run.steps))
        Observer().observe(run)
        self.assertEqual((run.status, run.current_index, len(run.steps)), before)

    def test_replan_bounded(self):
        ex = HarnessExecutor(store=InMemoryRunStore(), tool_executor=_exec())
        run = ex.create_run([{"tool": "a"}])
        for _ in range(MAX_REPLANS):
            ex.request_replan(run.run_id, [{"tool": "b"}])
        rec = Observer().observe(ex.store.get(run.run_id))
        self.assertEqual(rec.replans_remaining, 0)
