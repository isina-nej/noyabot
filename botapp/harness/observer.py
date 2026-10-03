"""Read-only harness observer (Agent E).

May RECOMMEND one of CONTINUE / WAIT / REPLAN / FINISH / FAIL.
May NOT authorize actions, execute tools, mutate runs, or touch the store.
Pure function of a run snapshot: no I/O, no clock, no imports from
botapp.agent / handlers / orchestrator / confirmations.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .limits import MAX_REPLANS, MAX_STEPS, MAX_TOOL_CALLS, MAX_TOTAL_RUNTIME_S
from .state import CANCELLED, FAILED, SUCCEEDED, WAITING_CONFIRMATION, HarnessRunState

CONTINUE = "CONTINUE"
WAIT = "WAIT"
REPLAN = "REPLAN"
FINISH = "FINISH"
FAIL = "FAIL"

VERDICTS = frozenset({CONTINUE, WAIT, REPLAN, FINISH, FAIL})


@dataclass(frozen=True)
class ObserverRecommendation:
    verdict: str
    reason: str = ""
    replans_used: int = 0
    replans_remaining: int = MAX_REPLANS


@dataclass(frozen=True)
class ObserverConfig:
    max_steps: int = MAX_STEPS
    max_tool_calls: int = MAX_TOOL_CALLS
    max_total_runtime_s: float = MAX_TOTAL_RUNTIME_S
    max_replans: int = MAX_REPLANS


class Observer:
    """Stateless, side-effect-free run reviewer."""

    def __init__(self, config: ObserverConfig | None = None):
        # ponytail: frozen dataclass config; upgrade to per-run overrides
        # only via ARCHITECTURE_CHANGE_REQUEST.
        self.config = config or ObserverConfig()

    def observe(self, run: HarnessRunState) -> ObserverRecommendation:
        snap = copy.deepcopy(run)  # read-only: never mutate the caller's object
        cfg = self.config
        remaining = max(0, cfg.max_replans - snap.replans_used)

        if snap.status == CANCELLED or snap.cancel_requested:
            return self._rec(FAIL, "cancelled", snap)
        if snap.status == SUCCEEDED:
            return self._rec(FINISH, "run succeeded", snap)
        if snap.status == FAILED:
            return self._rec(FAIL, snap.error or "run failed", snap)
        if snap.status == WAITING_CONFIRMATION:
            return self._rec(WAIT, "awaiting confirmation; wait excluded from runtime budget", snap)
        if snap.tool_calls_used >= cfg.max_tool_calls:
            return self._rec(FAIL, "tool_call_budget_exceeded", snap)
        if snap.active_runtime_s >= cfg.max_total_runtime_s:
            return self._rec(FAIL, "total_runtime_exceeded", snap)
        prev = snap.steps[snap.current_index - 1] if snap.current_index > 0 else None
        if prev is not None and prev.status == "failed" and remaining > 0:
            return self._rec(REPLAN, f"step {prev.step_id} failed; replan bounded ({remaining} left)", snap)
        if snap.current_index >= cfg.max_steps or snap.current_index >= len(snap.steps):
            ok = any(s.status == "succeeded" for s in snap.steps)
            if ok:
                return self._rec(FINISH, "plan exhausted with observations", snap)
            if not snap.steps:
                return self._rec(FINISH, "empty plan", snap)
            return self._rec(FAIL, "plan exhausted without success", snap)
        nxt = snap.steps[snap.current_index]
        if nxt.status == WAITING_CONFIRMATION:
            return self._rec(WAIT, f"step {nxt.step_id} awaiting confirmation", snap)
        if nxt.requires_confirmation:
            return self._rec(WAIT, f"step {nxt.step_id} requires confirmation", snap)
        return self._rec(CONTINUE, f"step {nxt.step_id} ready", snap)

    def _rec(self, verdict: str, reason: str, snap: HarnessRunState) -> ObserverRecommendation:
        assert verdict in VERDICTS
        return ObserverRecommendation(
            verdict=verdict,
            reason=reason,
            replans_used=snap.replans_used,
            replans_remaining=max(0, self.config.max_replans - snap.replans_used),
        )

    # Convenience: pure predicate for callers that only need a boolean.
    def should_continue(self, run: HarnessRunState) -> bool:
        return self.observe(run).verdict == CONTINUE


def recommend(run: HarnessRunState, config: ObserverConfig | None = None) -> dict[str, Any]:
    """Functional entry point returning a JSON-safe dict."""
    rec = Observer(config).observe(run)
    return {
        "verdict": rec.verdict,
        "reason": rec.reason,
        "replans_used": rec.replans_used,
        "replans_remaining": rec.replans_remaining,
    }
