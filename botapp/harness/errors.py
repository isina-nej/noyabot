"""PlanRejected taxonomy for the harness planning domain.

Pure domain: no Django, no Registry, no permissions, no Telegram.
Planner output is untrusted; every structural/contract failure surfaces
as PlanRejected with a stable code (audit-safe) and a user-safe Persian
message. Unknown codes raise ValueError, never silently default.
"""

from __future__ import annotations

REJECTED_CODES = frozenset(
    {
        "unknown_action",  # V1
        "invalid_schema",  # V2
        "denied_tool",  # V4 (unknown / non-allowlisted / self-call)
        "invalid_params",  # V6 (args must be {})
        "empty_code",  # V7
        "code_too_long",  # V7/V8 size
        "code_contract",  # V8 structural (must assign result)
        "empty_answer",  # V10 (finish without answer -> fallback)
        "max_steps_exceeded",  # V11
        "empty_plan",  # Plan has no steps
        "bad_kind",  # kind != harness.plan/v1
        "duplicate_step_id",  # DAG
        "unknown_dependency",  # DAG
        "self_dependency",  # DAG
        "cycle_detected",  # DAG
        "multiple_finish",  # >1 finish step
        "finish_not_terminal",  # another step depends on finish
    }
)

_FA_MESSAGES = {
    "unknown_action": "❌ اقدام برنامه‌ریز نامعتبر است.",
    "invalid_schema": "❌ ساختار برنامه بررسی معتبر نیست.",
    "denied_tool": "❌ این ابزار در بررسی مرحله‌ای مجاز نیست.",
    "invalid_params": "❌ پارامترهای گام برنامه معتبر نیست.",
    "empty_code": "❌ کد گام تحلیلی خالی است.",
    "code_too_long": "❌ کد گام تحلیلی بیش از حد طولانی است.",
    "code_contract": "❌ کد گام تحلیلی باید نتیجه را در result بگذارد.",
    "empty_answer": "❌ پاسخ پایانی خالی است.",
    "max_steps_exceeded": "❌ تعداد گام‌ها از سقف مجاز بیشتر است.",
    "empty_plan": "❌ برنامه بررسی خالی است.",
    "bad_kind": "❌ نوع برنامه پشتیبانی نمی‌شود.",
    "duplicate_step_id": "❌ شناسه گام تکراری است.",
    "unknown_dependency": "❌ وابستگی به گام ناشناخته است.",
    "self_dependency": "❌ گام نمی‌تواند به خودش وابسته باشد.",
    "cycle_detected": "❌ وابستگی‌های گام‌ها چرخه دارد.",
    "multiple_finish": "❌ برنامه فقط یک گام پایانی می‌تواند داشته باشد.",
    "finish_not_terminal": "❌ گام پایانی باید آخرین گام باشد.",
}


class PlanRejected(Exception):
    """Frozen rejection taxonomy. code is audit-safe, user_message is user-safe."""

    code = "plan_rejected"
    user_message = "❌ برنامه بررسی معتبر نیست."

    def __init__(self, user_message: str | None = None, *, code: str | None = None, step_id: str | None = None):
        if code is not None:
            if code not in REJECTED_CODES:
                raise ValueError(f"unknown rejection code: {code!r}")
            self.code = code
            if not user_message:
                user_message = _FA_MESSAGES[code]
        if user_message:
            self.user_message = user_message
        self.step_id = step_id
        super().__init__(self.user_message)
