"""Frozen harness runtime limits (Agent E).

Source of truth for executor/store/observer budgets.
Do not raise without ARCHITECTURE_CHANGE_REQUEST.
"""

MAX_STEPS = 8
DEFAULT_PLAN_STEPS = 5
MAX_REPLANS = 2
MAX_TOOL_CALLS = 12
MAX_TOTAL_RUNTIME_S = 90
MAX_RESULT_SIZE = 4000

# Per-step wall-clock default (not frozen; per-run override allowed).
# Must stay well under MAX_TOTAL_RUNTIME_S so several steps fit in budget.
PER_STEP_TIMEOUT_S = 20.0
MAX_STEP_RUNTIME_S = PER_STEP_TIMEOUT_S  # alias
DEFAULT_STEP_TIMEOUT_S = PER_STEP_TIMEOUT_S  # alias
