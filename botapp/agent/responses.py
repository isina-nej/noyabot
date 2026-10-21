"""Persian response formatting helpers for the admin agent."""

from __future__ import annotations

import re

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_INTERNAL_NOTE_RE = re.compile(
    r"^\s*(?:→|->)\s*skipped\s*:\s*.+\badd\s+when\s*:\s*.+$",
    re.IGNORECASE,
)


def clean_model_response(text: str) -> str:
    """Remove a known internal planning note without touching user content.

    The filter is line-anchored and disabled inside fenced code so arrows and
    programming examples remain intact.
    """
    lines: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        if not in_fence and _INTERNAL_NOTE_RE.fullmatch(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def fa_number(value: int | float) -> str:
    """Render an integer with Persian digits and the Persian thousands mark."""
    try:
        text = f"{int(value):,}"
    except (TypeError, ValueError):
        text = str(value)
    return text.translate(PERSIAN_DIGITS).replace(",", "٬")


def fa_duration(minutes: int | None) -> str:
    if minutes is None:
        return "دائمی"
    if minutes % 60 == 0 and minutes >= 60:
        return f"{fa_number(minutes // 60)} ساعت"
    return f"{fa_number(minutes)} دقیقه"


RISK_LABELS = {
    "low": "کم",
    "medium": "متوسط",
    "high": "زیاد",
}


def confirmation_preview(
    *,
    verb: str,
    target_label: str = "",
    target_user_id: int | None = None,
    duration_minutes: int | None = None,
    delay_minutes: int = 0,
    reason: str = "",
    value=None,
    requester_name: str = "",
    group_name: str = "",
    risk_level: str = "medium",
) -> str:
    lines = ["⚠️ تأیید عملیات مدیریتی", "", f"عملیات: {verb}"]
    if value is not None and str(value) != "":
        # Show the exact free-text/numeric value so the admin approves precisely
        # what will be applied (defends against injected/ambiguous values).
        lines.append(f"مقدار: {value}")
    if target_label:
        lines.append(f"کاربر: {target_label}")
    if target_user_id is not None:
        lines.append(f"شناسه: {fa_number(target_user_id)}")
    if delay_minutes:
        lines.append(f"تأخیر شروع: {fa_duration(delay_minutes)}")
    if duration_minutes is not None or verb.strip().endswith(("قفل", "سکوت", "محدودکردن کاربر")):
        lines.append(f"مدت: {fa_duration(duration_minutes)}")
    if reason:
        lines.append(f"دلیل: {reason}")
    if requester_name:
        lines.append(f"درخواست‌دهنده: {requester_name}")
    if group_name:
        lines.append(f"گروه: {group_name}")
    lines.append(f"سطح ریسک: {RISK_LABELS.get(risk_level, risk_level)}")
    lines.append("")
    lines.append("این عملیات انجام شود؟")
    return "\n".join(lines)
