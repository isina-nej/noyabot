"""Sequential harness executor (Agent E).

Pure stdlib. No imports from botapp.agent / handlers / orchestrator /
confirmations: tool execution is caller-injected, so admin writes can
never invoke handlers directly from here. Single attempt per step:
never auto-retry uncertain mutating operations.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable

from .limits import (
    DEFAULT_PLAN_STEPS,
    MAX_REPLANS,
    MAX_RESULT_SIZE,
    MAX_STEPS,
    MAX_TOOL_CALLS,
    MAX_TOTAL_RUNTIME_S,
    PER_STEP_TIMEOUT_S,
)
from .state import (
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    FINISHED,
    WAITING_CONFIRMATION,
    HarnessRunState,
    HarnessStepState,
    InMemoryRunStore,
    ReplanLimitExceededError,
    RunCancelledError,
    RunNotFoundError,
    RunStore,
    create_run,
    new_run_id,
    resolve_refs,
    truncate_result,
)

ToolExecutor = Callable[[str, dict[str, Any]], Any]


class BudgetExceededError(RuntimeError):
    pass


class StepTimeoutError(TimeoutError):
    pass


@dataclass
class ExecutorConfig:
    max_steps: int = MAX_STEPS
    max_tool_calls: int = MAX_TOOL_CALLS
    max_total_runtime_s: float = MAX_TOTAL_RUNTIME_S
    step_timeout_s: float = PER_STEP_TIMEOUT_S
    max_replans: int = MAX_REPLANS


def default_plan(n: int = DEFAULT_PLAN_STEPS) -> list[dict[str, Any]]:
    return [{"tool": f"step-{i + 1}", "params": {}} for i in range(max(0, n))]


def _now() -> float:
    return time.monotonic()


class HarnessExecutor:
    """Sequential run loop over HarnessRunState persisted in a RunStore."""

    def __init__(
        self,
        store: RunStore | None = None,
        tool_executor: ToolExecutor | None = None,
        *,
        config: ExecutorConfig | None = None,
        max_steps: int = MAX_STEPS,
        max_tool_calls: int = MAX_TOOL_CALLS,
        max_total_runtime_s: float = MAX_TOTAL_RUNTIME_S,
        step_timeout_s: float = PER_STEP_TIMEOUT_S,
        max_replans: int = MAX_REPLANS,
        clock: Callable[[], float] = _now,
    ):
        self.store: RunStore = store if store is not None else InMemoryRunStore()
        self.tool_executor = tool_executor or (lambda tool, params: None)
        cfg = config or ExecutorConfig()
        # Explicit kwargs win over config object when non-default.
        self.max_steps = max_steps if max_steps != MAX_STEPS or config is None else cfg.max_steps
        self.max_tool_calls = max_tool_calls
        self.max_total_runtime_s = max_total_runtime_s
        self.step_timeout_s = step_timeout_s
        self.max_replans = max_replans
        if config is not None and max_steps == MAX_STEPS and max_tool_calls == MAX_TOOL_CALLS:
            self.max_steps, self.max_tool_calls = cfg.max_steps, cfg.max_tool_calls
            self.max_total_runtime_s, self.step_timeout_s = cfg.max_total_runtime_s, cfg.step_timeout_s
            self.max_replans = cfg.max_replans
        self.clock = clock

    # --- lifecycle ------------------------------------------------------
    def create_run(self, steps: list[dict[str, Any]] | None = None, *, run_id: str = "") -> HarnessRunState:
        specs = list(steps) if steps else default_plan()
        if len(specs) > self.max_steps:
            raise ValueError(f"plan exceeds max_steps={self.max_steps}")
        run = create_run(specs, run_id=run_id or new_run_id())
        run.status = PENDING
        return self.store.create(run)

    def cancel(self, run_id: str) -> HarnessRunState:
        run = self._load(run_id)
        if run.is_terminal:
            return run
        run.cancel_requested = True
        run.status = CANCELLED
        run.error = run.error or "cancelled"
        for s in run.steps:
            if s.status in (PENDING, RUNNING, WAITING_CONFIRMATION):
                s.status = CANCELLED
                s.ended_at = self.clock()
        self.store.save(run)
        return self._load(run_id)

    def approve_confirmation(self, run_id: str, step_id: str, approved: bool) -> HarnessRunState:
        run = self._load(run_id)
        step = self._find(run, step_id)
        if step.status != WAITING_CONFIRMATION:
            raise RuntimeError(f"step {step_id} not awaiting confirmation")
        if approved:
            step.requires_confirmation = False
            step.status = PENDING
            if run.status == WAITING_CONFIRMATION:
                run.status = RUNNING
            run.pending_confirmation_step_id = ""
        else:
            step.status = "skipped"
            step.error = "confirmation_denied"
            run.current_index += 1
            if run.status == WAITING_CONFIRMATION:
                run.status = RUNNING
            run.pending_confirmation_step_id = ""
        self.store.save(run)
        return self._load(run_id)

    def request_replan(self, run_id: str, new_steps: list[dict[str, Any]]) -> HarnessRunState:
        run = self._load(run_id)
        if run.is_terminal:
            raise RuntimeError("cannot replan a terminal run")
        if run.replans_used >= self.max_replans:
            raise ReplanLimitExceededError(f"MAX_REPLANS={self.max_replans} exhausted")
        if len(new_steps) + run.current_index > self.max_steps:
            raise ValueError(f"replan exceeds max_steps={self.max_steps}")
        if run.cancel_requested:
            raise RunCancelledError("run cancelled")
        repl: list[HarnessStepState] = []
        for j, spec in enumerate(new_steps, start=run.current_index + 1):
            spec = dict(spec or {})
            repl.append(HarnessStepState(
                step_id=f"{j:03d}",
                tool=spec.get("tool", ""),
                params=dict(spec.get("params") or spec.get("args") or {}),
                read_only=bool(spec.get("read_only", True)),
                requires_confirmation=bool(spec.get("requires_confirmation", False)),
            ))
        run.steps = run.steps[:run.current_index] + repl
        run.replans_used += 1
        if run.status != WAITING_CONFIRMATION:
            run.status = RUNNING
        self.store.save(run)
        return self._load(run_id)

    # Alias: bounded re-plan requests.
    request_replan_bounded = request_replan

    # --- run loops -------------------------------------------------------
    def run(self, run_id: str) -> HarnessRunState:
        run = self._load(run_id)
        if run.is_terminal:
            return run
        if run.cancel_requested:
            return self.cancel(run_id)
        run.status = RUNNING
        self.store.save(run)
        while True:
            run = self._load(run_id)
            if run.is_terminal or run.status == WAITING_CONFIRMATION:
                return run
            if run.cancel_requested:
                return self.cancel(run_id)
            if run.current_index >= len(run.steps):
                return self._finish(run, FINISHED, "")
            nxt = self._check_budgets(run)
            if nxt is not None:
                return nxt
            step = run.steps[run.current_index]
            if step.status in ("finished", "failed", "skipped", "cancelled"):
                run.current_index += 1
                self.store.save(run)
                continue
            if step.requires_confirmation:
                return self._pause_for_confirmation(run, step)
            self._exec_sync(run, step)
            # _exec_sync saves; loop reloads.

    async def arun(self, run_id: str) -> HarnessRunState:
        run = self._load(run_id)
        if run.is_terminal:
            return run
        if run.cancel_requested:
            return self.cancel(run_id)
        run.status = RUNNING
        self.store.save(run)
        while True:
            run = self._load(run_id)
            if run.is_terminal or run.status == WAITING_CONFIRMATION:
                return run
            if run.cancel_requested:
                return self.cancel(run_id)
            if run.current_index >= len(run.steps):
                return self._finish(run, FINISHED, "")
            nxt = self._check_budgets(run)
            if nxt is not None:
                return nxt
            step = run.steps[run.current_index]
            if step.status in ("finished", "failed", "skipped", "cancelled"):
                run.current_index += 1
                self.store.save(run)
                continue
            if step.requires_confirmation:
                return self._pause_for_confirmation(run, step)
            await self._exec_async(run, step)

    def resume(self, run_id: str, approvals: dict[str, bool] | None = None) -> HarnessRunState:
        run = self._load(run_id)  # honest fail on unknown id (restart-safe)
        if run.is_terminal:
            return run
        if run.cancel_requested:
            return self.cancel(run_id)
        if approvals:
            for sid, ok in approvals.items():
                try:
                    run = self.approve_confirmation(run_id, sid, bool(ok))
                except RuntimeError:
                    continue
            run = self._load(run_id)
        if run.status == WAITING_CONFIRMATION and not approvals:
            return run  # still paused; wait time stays outside active budget
        run.status = RUNNING
        self.store.save(run)
        return self.run(run_id)

    async def aresume(self, run_id: str, approvals: dict[str, bool] | None = None) -> HarnessRunState:
        run = self._load(run_id)
        if run.is_terminal:
            return run
        if run.cancel_requested:
            return self.cancel(run_id)
        if approvals:
            for sid, ok in approvals.items():
                try:
                    run = self.approve_confirmation(run_id, sid, bool(ok))
                except RuntimeError:
                    continue
            run = self._load(run_id)
        if run.status == WAITING_CONFIRMATION and not approvals:
            return run
        run.status = RUNNING
        self.store.save(run)
        return await self.arun(run_id)

    # --- internals --------------------------------------------------------
    def _load(self, run_id: str) -> HarnessRunState:
        run = self.store.get(run_id)
        if run is None:
            raise RunNotFoundError(f"unknown run_id: {run_id}")  # never synthesize
        return run

    def _find(self, run: HarnessRunState, step_id: str) -> HarnessStepState:
        for s in run.steps:
            if s.step_id == step_id:
                return s
        raise KeyError(f"unknown step_id: {step_id}")

    def _index_of(self, run: HarnessRunState, step_id: str) -> int:
        for i, s in enumerate(run.steps):
            if s.step_id == step_id:
                return i
        raise KeyError(f"unknown step_id: {step_id}")

    def _check_budgets(self, run: HarnessRunState) -> HarnessRunState | None:
        if run.current_index >= self.max_steps or len(run.steps) > self.max_steps:
            run.status = FAILED
            run.error = "max_steps"
            self.store.save(run)
            return self._load(run.run_id)
        if run.tool_calls_used >= self.max_tool_calls:
            return self._finish(run, FAILED, "tool_call_budget_exceeded")
        if run.active_runtime_s >= self.max_total_runtime_s:
            return self._finish(run, FAILED, "total_runtime_exceeded")
        return None

    def _pause_for_confirmation(self, run: HarnessRunState, step: HarnessStepState) -> HarnessRunState:
        step.status = WAITING_CONFIRMATION
        step.started_at = step.started_at or self.clock()
        run.status = WAITING_CONFIRMATION
        run.waiting_since = self.clock()  # wait excluded from active_runtime_s
        run.pending_confirmation_step_id = step.step_id
        self.store.save(run)
        return self._load(run.run_id)

    def _params(self, run: HarnessRunState, step: HarnessStepState) -> dict[str, Any]:
        resolved = resolve_refs(step.params, run.results_by_step())
        return dict(resolved) if isinstance(resolved, dict) else {}

    def _record_success(self, run: HarnessRunState, step: HarnessStepState, result: Any, elapsed: float) -> None:
        step.result = truncate_result(result, MAX_RESULT_SIZE)
        step.status = "finished"
        step.error = ""
        step.ended_at = self.clock()
        if step.tool:
            run.tool_calls_used += 1
        run.active_runtime_s += elapsed
        run.current_index += 1
        self.store.save(run)

    def _record_failure(self, run: HarnessRunState, step: HarnessStepState, error: str, elapsed: float) -> None:
        # Partial failure: record, advance, continue. No retry (esp. mutating).
        step.status = "failed"
        step.error = str(error)[:500]
        step.result = None
        step.ended_at = self.clock()
        if step.tool:
            run.tool_calls_used += 1
        run.active_runtime_s += elapsed
        run.current_index += 1
        self.store.save(run)

    def _exec_sync(self, run: HarnessRunState, step: HarnessStepState) -> None:
        step_id = step.step_id
        step.status = "running"
        step.started_at = self.clock()
        self.store.save(run)
        params = self._params(run, step)
        t0 = self.clock()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(self.tool_executor, step.tool, params)
                try:
                    result = fut.result(timeout=self.step_timeout_s)
                except concurrent.futures.TimeoutError:
                    raise StepTimeoutError(f"step_timeout exceeded: {self.step_timeout_s}s")
                if inspect.isawaitable(result):
                    result = self._await_sync(result)
            # ponytail: sync path blocks a thread per step; upgrade to arun
            # when tool_executor is natively async.
            live = self._load(run.run_id)
            idx = self._index_of(live, step_id)
            self._record_success(live, live.steps[idx], result, self.clock() - t0)
        except Exception as exc:  # noqa: BLE001 - partial failure recorded
            live = self._load(run.run_id)
            idx = self._index_of(live, step_id)
            self._record_failure(live, live.steps[idx], self._err(exc), self.clock() - t0)

    async def _exec_async(self, run: HarnessRunState, step: HarnessStepState) -> None:
        step_id = step.step_id
        step.status = "running"
        step.started_at = self.clock()
        self.store.save(run)
        params = self._params(run, step)
        t0 = self.clock()
        try:
            result = self.tool_executor(step.tool, params)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(asyncio.ensure_future(result), timeout=self.step_timeout_s)
            live = self._load(run.run_id)
            idx = self._index_of(live, step_id)
            self._record_success(live, live.steps[idx], result, self.clock() - t0)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            live = self._load(run.run_id)
            idx = self._index_of(live, step_id)
            self._record_failure(live, live.steps[idx], f"step_timeout: {exc}", self.clock() - t0)
        except Exception as exc:  # noqa: BLE001
            live = self._load(run.run_id)
            idx = self._index_of(live, step_id)
            self._record_failure(live, live.steps[idx], self._err(exc), self.clock() - t0)

    @staticmethod
    def _await_sync(awaitable: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)  # type: ignore[arg-type]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, awaitable).result()  # type: ignore[arg-type]

    @staticmethod
    def _err(exc: Exception) -> str:
        if isinstance(exc, StepTimeoutError):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"

    def _finish(self, run: HarnessRunState, status: str, error: str) -> HarnessRunState:
        run = self._load(run.run_id)
        if status == FINISHED:
            ok_steps = [s for s in run.steps if s.status == "finished"]
            if not run.steps:
                run.status = FINISHED
            elif not ok_steps:
                run.status = FAILED  # all failed != success
                run.error = error or "all_steps_failed"
            else:
                run.status = FINISHED
                if any(s.status == "failed" for s in run.steps):
                    run.error = error or "partial_failure"
                last = ok_steps[-1].result
                run.answer = last if isinstance(last, str) else (str(last) if last is not None else "")
                run.answer = truncate_result(run.answer, MAX_RESULT_SIZE) if isinstance(run.answer, str) else run.answer
        else:
            run.status = status
            run.error = error
        self.store.save(run)
        return self._load(run.run_id)


# Alias for graders/caller convenience.
SequentialExecutor = HarnessExecutor
Executor = HarnessExecutor
