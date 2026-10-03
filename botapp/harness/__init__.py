"""Harness planning domain (Agent B).

Pure planning: schemas, DAG ordering, deterministic validation (V1-V12
structural half), planner interface, deterministic fallback.

No tool execution, no Telegram routing, no permissions, no confirmation,
no Registry/Orchestrator/settings access. Planner output is untrusted;
validate before the Security gate.
"""

from .errors import PlanRejected
from .planner import AIPlannerAdapter, Planner, PlannerResult, deterministic_plan
from .plans import topological_order, validate_dag
from .schemas import (
    FALLBACK_ITEM_CHARS,
    FALLBACK_WINDOW_ITEMS,
    MAX_ANSWER_CHARS,
    MAX_CODE_CHARS,
    MAX_COMMAND_CHARS,
    MAX_MEMORY_ITEMS,
    MAX_OBSERVATION_CHARS,
    MAX_STEP_ID_CHARS,
    MAX_STEPS_CODE_DEFAULT,
    MAX_STEPS_SETTING_DEFAULT,
    MAX_THINKING_CHARS,
    PLAN_KIND,
    SANDBOX_TIMEOUT_SECONDS,
    SELF_TOOL_NAME,
    DETERMINISTIC_TOOLS,
    Budget,
    Plan,
    PlanStep,
    TargetSource,
    deterministic_plan_steps,
    resolve_max_steps,
    to_harness_step,
)
from .validation import StepNote, ValidationResult, truncate, validate_plan, validate_raw_step

__all__ = [
    "AIPlannerAdapter",
    "Budget",
    "DETERMINISTIC_TOOLS",
    "FALLBACK_ITEM_CHARS",
    "FALLBACK_WINDOW_ITEMS",
    "MAX_ANSWER_CHARS",
    "MAX_CODE_CHARS",
    "MAX_COMMAND_CHARS",
    "MAX_MEMORY_ITEMS",
    "MAX_OBSERVATION_CHARS",
    "MAX_STEP_ID_CHARS",
    "MAX_STEPS_CODE_DEFAULT",
    "MAX_STEPS_SETTING_DEFAULT",
    "MAX_THINKING_CHARS",
    "PLAN_KIND",
    "SANDBOX_TIMEOUT_SECONDS",
    "SELF_TOOL_NAME",
    "Plan",
    "PlanRejected",
    "PlanStep",
    "Planner",
    "PlannerResult",
    "StepNote",
    "TargetSource",
    "deterministic_plan",
    "deterministic_plan_steps",
    "resolve_max_steps",
    "to_harness_step",
    "topological_order",
    "truncate",
    "validate_dag",
    "validate_plan",
    "validate_raw_step",
]
