"""Harness package (Phase 1 integration).

Union of planning domain (Agent B), capability catalog (Agent C),
security gate (Agent D), and runtime executor/store/observer (Agent E).

Planning: schemas, DAG ordering, deterministic validation (V1-V12
structural half), planner interface, deterministic fallback.
Planner output is untrusted; validate before the Security gate.

Runtime: state/store, executor, observer, limits.
"""

from .errors import PlanRejected
from .executor import Executor, ExecutorConfig, HarnessExecutor, SequentialExecutor, default_plan
from .limits import (
    DEFAULT_PLAN_STEPS,
    DEFAULT_STEP_TIMEOUT_S,
    MAX_REPLANS,
    MAX_RESULT_SIZE,
    MAX_STEP_RUNTIME_S,
    MAX_STEPS,
    MAX_TOOL_CALLS,
    MAX_TOTAL_RUNTIME_S,
    PER_STEP_TIMEOUT_S,
)
from .observer import CONTINUE, FAIL, FINISH, REPLAN, WAIT, Observer, ObserverConfig, ObserverRecommendation, recommend
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
from .state import (
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    FINISHED,
    WAITING_CONFIRMATION,
    BudgetExceededError,
    HarnessRunState,
    HarnessStepState,
    InMemoryRunStore,
    ReplanLimitExceededError,
    ResultRef,
    RunCancelledError,
    RunNotFoundError,
    RunStatus,
    RunStore,
    StepRef,
    StepStatus,
    create_run,
    new_run_id,
    resolve_refs,
    truncate_result,
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
    "Executor", "ExecutorConfig", "HarnessExecutor", "SequentialExecutor", "default_plan",
    "DEFAULT_PLAN_STEPS", "DEFAULT_STEP_TIMEOUT_S", "MAX_REPLANS", "MAX_RESULT_SIZE",
    "MAX_STEP_RUNTIME_S", "MAX_STEPS", "MAX_TOOL_CALLS", "MAX_TOTAL_RUNTIME_S", "PER_STEP_TIMEOUT_S",
    "CONTINUE", "FAIL", "FINISH", "REPLAN", "WAIT", "Observer", "ObserverConfig",
    "ObserverRecommendation", "recommend",
    "CANCELLED", "FAILED", "FINISHED", "PENDING", "RUNNING", "WAITING_CONFIRMATION",
    "BudgetExceededError", "HarnessRunState", "HarnessStepState", "InMemoryRunStore",
    "ReplanLimitExceededError", "ResultRef", "RunCancelledError", "RunNotFoundError",
    "RunStatus", "RunStore", "StepRef", "StepStatus", "create_run", "new_run_id",
    "resolve_refs", "truncate_result",
]
