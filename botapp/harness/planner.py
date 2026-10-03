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
    from .plans import topological_order

    cap = max_steps if max_steps is not None else resolve_max_steps(setting)
    steps = deterministic_plan_steps()
    # ponytail: deterministic pass runs full 9-step chain regardless of
    # cap; budget raised to fit. Add when: cap-truncated deterministic pass.
    cap = max(cap, len(steps))
    ordered = topological_order(steps)
    plan = Plan(
        kind=PLAN_KIND,
        budget={"max_steps": cap},  # type: ignore[arg-type]
        steps=[s.model_dump() for s in ordered],  # type: ignore[arg-type]
    )
    return PlannerResult(plan=plan, error="deterministic")


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
