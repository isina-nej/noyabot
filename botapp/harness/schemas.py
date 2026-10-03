"""Planning schemas for the harness domain (frozen §3, §10, §15).

Pure: stdlib + pydantic only. No Django, no Registry, no permissions,
no Telegram, no execution. Planner output is untrusted; every payload
here is validated before the Security gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLAN_KIND = "harness.plan/v1"
SELF_TOOL_NAME = "harness.investigate"

DETERMINISTIC_TOOLS = (
    "analytics.get_message_activity_today",
    "analytics.get_top_senders_today",
    "analytics.get_today_summary",
    "group.get_member_count",
    "group.get_moderation_status",
    "group.get_schedules",
    "audit.get_recent_actions",
)

DETERMINISTIC_CODE = (
    "nums = []\n"
    "for item in data.get('items') or []:\n"
    "    text = str(item.get('summary') or '')\n"
    "    for token in text.replace('،', ' ').split():\n"
    "        t = token.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))\n"
    "        if t.isdigit():\n"
    "            nums.append(int(t))\n"
    "result = {'observation_count': data.get('count', 0), 'numbers_seen': nums[:40]}\n"
)

MAX_THINKING_CHARS = 400
MAX_CODE_CHARS = 4000
MAX_ANSWER_CHARS = 4000
MAX_OBSERVATION_CHARS = 3500
MAX_MEMORY_ITEMS = 12
FALLBACK_WINDOW_ITEMS = 8
FALLBACK_ITEM_CHARS = 500
MAX_COMMAND_CHARS = 2000
MAX_STEP_ID_CHARS = 64
MAX_STEPS_SETTING_DEFAULT = 8
MAX_STEPS_CODE_DEFAULT = 5
SANDBOX_TIMEOUT_SECONDS = 2.0

TargetSource = Literal["reply", "none"]


class Budget(BaseModel):
    """Step budget. Upper bound is the frozen setting/code default chain."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(ge=1)


class PlanStep(BaseModel):
    """One DAG node. args accepted but Phase 1 forces {} (V6)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=MAX_STEP_ID_CHARS)
    depends_on: list[str] = Field(default_factory=list)
    action: Literal["call_tool", "run_code", "finish"]
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(default="", max_length=MAX_CODE_CHARS)
    answer: str = Field(default="", max_length=MAX_ANSWER_CHARS)
    thinking: str = Field(default="", max_length=MAX_THINKING_CHARS)
    target_source: TargetSource = "none"

    @field_validator("id", "tool", "code", "answer", "thinking")
    @classmethod
    def _strip(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("depends_on")
    @classmethod
    def _strip_deps(cls, value: list[str]) -> list[str]:
        return [(v or "").strip() for v in (value or []) if (v or "").strip()]


class Plan(BaseModel):
    """Frozen plan envelope. kind must be harness.plan/v1."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["harness.plan/v1"] = PLAN_KIND
    budget: Budget
    steps: list[PlanStep] = Field(min_length=1)


def resolve_max_steps(setting: Any = None) -> int:
    """Frozen §10 chain: absent -> 5, present -> int(setting)."""
    from .errors import PlanRejected

    if setting is None:
        return MAX_STEPS_CODE_DEFAULT
    if isinstance(setting, bool):
        raise PlanRejected(code="invalid_schema")
    try:
        value = int(setting)
    except (TypeError, ValueError):
        raise PlanRejected(code="invalid_schema") from None
    if value < 1:
        raise PlanRejected(code="max_steps_exceeded")
    return value


def deterministic_plan_steps() -> list[PlanStep]:
    """Frozen §3 fallback: read tools in fixed order, code pass, finish.

    Finish answer is empty on purpose: V10 routes it to _fallback_answer
    plus the provenance footer at answer time.
    """
    steps: list[PlanStep] = []
    prev: str | None = None
    for i, name in enumerate(DETERMINISTIC_TOOLS):
        sid = f"s{i + 1:02d}"
        steps.append(
            PlanStep(
                id=sid,
                depends_on=[prev] if prev else [],
                action="call_tool",
                tool=name,
                thinking="گردآوری خواندنی قطعی",
            )
        )
        prev = sid
    code_id = f"s{len(steps) + 1:02d}"
    steps.append(
        PlanStep(
            id=code_id,
            depends_on=[prev] if prev else [],
            action="run_code",
            code=DETERMINISTIC_CODE.strip(),
            thinking="استخراج عددهای مشاهده‌ها",
        )
    )
    steps.append(
        PlanStep(
            id=f"s{len(steps) + 1:02d}",
            depends_on=[code_id],
            action="finish",
            answer="",
            thinking="جمع‌بندی قطعی",
        )
    )
    return steps


def to_harness_step(step: PlanStep) -> dict[str, Any]:
    """Project to the runtime HarnessStep shape. args forced to {} (V6)."""
    return {
        "action": step.action,
        "tool": step.tool,
        "args": {},
        "code": step.code,
        "answer": step.answer,
        "thinking": step.thinking[:MAX_THINKING_CHARS],
    }
