"""Planner interface + AI adapter boundary + deterministic fallback.

Pure: stdlib only. No Registry, no permissions, no execution, no
Telegram, no settings. AI output is untrusted: validate before
the Security gate. Target confinement: reply | none only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .schemas import (
    MAX_STEPS_CODE_DEFAULT,
    PLAN_KIND,
    Plan,
    PlanStep,
    deterministic_plan_steps,
    resolve_max_steps,
)


@dataclass
class PlannerResult:
    plan: Plan
    error: str = ""  # "" | "deterministic"


class Planner(Protocol):
    def plan(self, *, user_text: str, allowlist: list[str], max_steps: int) -> PlannerResult: ...


def deterministic_plan(*, max_steps: int | None = None, setting: Any = None) -> PlannerResult:
    """Frozen §3 fallback: read tools, code pass, empty finish (V10)."""
    from .limits import MAX_STEPS
    from .plans import topological_order
    from .schemas import executable_step_count

    cap = max_steps if max_steps is not None else resolve_max_steps(setting)
    # Frozen request-wide ceiling: total nodes (incl. the terminal finish
    # marker) never exceed MAX_STEPS=8, so executable nodes fit too. The
    # frozen 7-tool chain (9 nodes) truncates at the tail in frozen order
    # (finish marker always kept); smaller caps truncate further. No budget
    # inflation, no executor special-case, no tool step excluded from
    # accounting.
    cap = min(cap, MAX_STEPS)
    steps = deterministic_plan_steps()
    if len(steps) > cap:
        # Total nodes (incl. the terminal finish marker) fit the frozen cap;
        # executable nodes therefore fit too. Keep the head of the frozen
        # chain in order; prefer retaining the code pass + finish marker so
        # sandbox + finalization semantics stay covered.
        exec_nodes = [s for s in steps if s.action in ("call_tool", "run_code")]
        code = [s for s in exec_nodes if s.action == "run_code"][:1]
        tools = [s for s in exec_nodes if s.action == "call_tool"]
        keep_tools = max(cap - len(code) - 1, 0)  # -1 finish marker
        exec_keep = tools[:keep_tools] + code
        steps = _rebuild(steps, exec_keep)
    ordered = topological_order(steps)
    plan = Plan(
        kind=PLAN_KIND,
        budget={"max_steps": cap},  # type: ignore[arg-type]
        steps=[s.model_dump() for s in ordered],  # type: ignore[arg-type]
    )
    return PlannerResult(plan=plan, error="deterministic")


def _rebuild(all_steps: "list[PlanStep]", exec_keep: "list[PlanStep]") -> "list[PlanStep]":
    """Rechain kept executable nodes (frozen order) + the finish marker."""
    finish = [s for s in all_steps if s.action == "finish"][:1]
    kept = list(exec_keep) + finish
    out: list[PlanStep] = []
    prev: str | None = None
    for i, s in enumerate(kept):
        d = s.model_dump()
        d["id"] = f"s{i + 1:02d}"
        d["depends_on"] = [prev] if prev else []
        out.append(PlanStep.model_validate(d))
        prev = f"s{i + 1:02d}"
    return out


@dataclass
class AIPlannerAdapter:
    """Boundary: raw single-step dict -> PlanStep V3/V10 truncate only.

    No network, no settings, no execution. Anything structurally wrong
    raises PlanRejected; runtime/Security re-checks V4 allowlist + V5
    dual-gate + V6 params at step time.
    """

    notes: list[dict[str, str]] = field(default_factory=list)

    def parse_step(self, payload: dict[str, Any], *, step_id: str = "s01") -> PlanStep:
        from .validation import validate_raw_step

        return validate_raw_step(payload, step_id=step_id)

    def to_steps(self, payloads: list[dict[str, Any]]) -> list[PlanStep]:
        return [self.parse_step(p, step_id=f"s{i + 1:02d}") for i, p in enumerate(payloads)]

    def max_steps_or_default(self, setting: Any = None) -> int:
        try:
            return resolve_max_steps(setting)
        except Exception:
            return MAX_STEPS_CODE_DEFAULT
