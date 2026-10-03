"""Deterministic validator (structural half of frozen V1-V12).

Pure: stdlib + pydantic only. No Registry, no permissions, no execution,
no Telegram, no settings. Planner output is untrusted.

Covers structurally: V1 action enum, V2 schema shape, V3 thinking
truncate (never reject), V4 self-call + optional allowlist membership,
V6 args must be {}, V7 code non-empty/size, V8 result-assignment
contract (full sandbox gates belong to Agent E), V10 finish answer
(empty rejects, overlong truncates), V11 budget cap, V12 truncate
helper. V5 dual-gate + live tool execution belong to runtime/Security,
never here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .plans import topological_order, validate_dag
from .schemas import (
    MAX_ANSWER_CHARS,
    MAX_CODE_CHARS,
    MAX_OBSERVATION_CHARS,
    MAX_THINKING_CHARS,
    PLAN_KIND,
    SELF_TOOL_NAME,
    Plan,
    PlanStep,
)

_ACTIONS = ("call_tool", "run_code", "finish")


@dataclass
class StepNote:
    step_id: str
    code: str
    summary: str


@dataclass
class ValidationResult:
    steps: list[PlanStep] = field(default_factory=list)
    notes: list[StepNote] = field(default_factory=list)
    error: str = ""


def truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[truncated]"


def _has_result_assignment(code: str) -> bool:
    """V8 structural: snippet must assign `result`. No execution."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "result" and isinstance(node.ctx, ast.Store):
            return True
    return False


def _check_step(step: PlanStep, allowlist: list[str] | None, notes: list[StepNote]) -> None:
    from .errors import PlanRejected

    if step.action not in _ACTIONS:  # pragma: no cover - schema enforces
        raise PlanRejected(code="unknown_action", step_id=step.id)
    if step.action == "call_tool":
        if not step.tool:
            raise PlanRejected(code="denied_tool", step_id=step.id)
        if step.tool == SELF_TOOL_NAME:
            raise PlanRejected(code="denied_tool", step_id=step.id)
        if allowlist is not None and step.tool not in allowlist:
            raise PlanRejected(code="denied_tool", step_id=step.id)
        if step.args != {}:
            raise PlanRejected(code="invalid_params", step_id=step.id)
    elif step.action == "run_code":
        if not step.code:
            raise PlanRejected(code="empty_code", step_id=step.id)
        if len(step.code) > MAX_CODE_CHARS:  # pragma: no cover - schema enforces
            raise PlanRejected(code="code_too_long", step_id=step.id)
        # ponytail: AST import/attr gates live in Agent E sandbox.py;
        # here only the result-assignment contract. Upgrade: none here.
        if not _has_result_assignment(step.code):
            raise PlanRejected(code="code_contract", step_id=step.id)
    else:  # finish
        if not step.answer:
            raise PlanRejected(code="empty_answer", step_id=step.id)


def _preprocess_raw_step(raw: dict[str, Any], notes: list[StepNote], sid: str) -> dict[str, Any]:
    data = dict(raw)
    thinking = data.get("thinking", "")
    if isinstance(thinking, str) and len(thinking.strip()) > MAX_THINKING_CHARS:
        data["thinking"] = thinking.strip()[:MAX_THINKING_CHARS]
        notes.append(StepNote(step_id=sid, code="truncated", summary="thinking truncated (V3)"))
    answer = data.get("answer", "")
    if isinstance(answer, str) and len(answer.strip()) > MAX_ANSWER_CHARS:
        data["answer"] = answer.strip()[:MAX_ANSWER_CHARS]
        notes.append(StepNote(step_id=sid, code="truncated", summary="answer truncated (V10)"))
    return data


def validate_raw_step(payload: dict[str, Any], *, step_id: str = "s01") -> PlanStep:
    """Validate one untrusted HarnessStep-shaped dict. Raises PlanRejected."""
    from .errors import PlanRejected

    if not isinstance(payload, dict):
        raise PlanRejected(code="invalid_schema", step_id=step_id)
    action = str(payload.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        raise PlanRejected(code="unknown_action", step_id=step_id)
    notes: list[StepNote] = []
    data = _preprocess_raw_step(payload, notes, step_id)
    shaped = {
        "id": str(data.get("id") or step_id).strip() or step_id,
        "depends_on": data.get("depends_on", []),
        "action": action,
        "tool": data.get("tool", ""),
        "args": data.get("args", {}),
        "code": data.get("code", ""),
        "answer": data.get("answer", ""),
        "thinking": data.get("thinking", ""),
        "target_source": data.get("target_source", "none"),
    }
    if not isinstance(shaped["args"], dict):
        raise PlanRejected(code="invalid_params", step_id=step_id)
    try:
        step = PlanStep.model_validate(shaped)
    except ValidationError as exc:
        for err in exc.errors():
            loc = str(err.get("loc", ""))
            if "action" in loc:
                raise PlanRejected(code="unknown_action", step_id=step_id) from None
            if "code" in loc:
                raise PlanRejected(code="code_too_long", step_id=step_id) from None
        raise PlanRejected(code="invalid_schema", step_id=step_id) from None
    _check_step(step, None, notes)
    return step


def validate_plan(
    payload: dict[str, Any],
    *,
    allowlist: list[str] | None = None,
    max_steps: int | None = None,
    allow_empty_finish: bool = False,
) -> ValidationResult:
    """Validate untrusted full-plan dict. Raises PlanRejected. No execution."""
    from .errors import PlanRejected

    if not isinstance(payload, dict):
        raise PlanRejected(code="invalid_schema")
    if payload.get("kind", PLAN_KIND) != PLAN_KIND:
        raise PlanRejected(code="bad_kind")
    notes: list[StepNote] = []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanRejected(code="empty_plan")
    cleaned: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PlanRejected(code="invalid_schema")
        sid = str(raw.get("id") or f"s{i + 1:02d}").strip() or f"s{i + 1:02d}"
        data = _preprocess_raw_step(raw, notes, sid)
        action = str(data.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            raise PlanRejected(code="unknown_action", step_id=sid)
        shaped = {
            "id": sid,
            "depends_on": data.get("depends_on", []),
            "action": action,
            "tool": data.get("tool", ""),
            "args": data.get("args", {}),
            "code": data.get("code", ""),
            "answer": data.get("answer", ""),
            "thinking": data.get("thinking", ""),
            "target_source": data.get("target_source", "none"),
        }
        if not isinstance(shaped["args"], dict):
            raise PlanRejected(code="invalid_params", step_id=sid)
        cleaned.append(shaped)
    try:
        plan = Plan.model_validate({"kind": PLAN_KIND, "budget": payload.get("budget"), "steps": cleaned})
    except ValidationError as exc:
        for err in exc.errors():
            loc = str(err.get("loc", ""))
            if "action" in loc:
                raise PlanRejected(code="unknown_action") from None
            if "code" in loc:
                raise PlanRejected(code="code_too_long") from None
        errs = exc.errors()
        if any("steps" in str(e.get("loc", "")) and "min" in str(e.get("type", "")) for e in errs):
            raise PlanRejected(code="empty_plan") from None
        raise PlanRejected(code="invalid_schema") from None
    for step in plan.steps:
        if step.action == "finish" and not step.answer and allow_empty_finish:
            continue
        _check_step(step, allowlist, notes)
    validate_dag(plan.steps)
    if len(plan.steps) > plan.budget.max_steps:
        raise PlanRejected(code="max_steps_exceeded")
    if max_steps is not None and len(plan.steps) > max_steps:
        raise PlanRejected(code="max_steps_exceeded")
    return ValidationResult(steps=topological_order(plan.steps), notes=notes)
