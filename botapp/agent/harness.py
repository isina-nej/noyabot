"""
ReAct-style investigation harness for the Telegram admin agent.

Flow per user request:
  1. Gather facts via LOW/read tools (auto-execute, no confirmation).
  2. Optionally run sandboxed Python on the gathered observations.
  3. Finish with a Persian answer grounded in observations.

HIGH/write tools are never auto-run here — those stay on the confirmation path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from .ai import ask_harness_step
from .permissions import ensure_admin_permission, ensure_bot_capability
from .registry import registry
from .risk import LOW
from .sandbox import run_sandboxed
from botapp.observability.instrumentation import instrument_async

logger = logging.getLogger("botapp.agent")

MAX_STEPS_DEFAULT = 5
MAX_OBSERVATION_CHARS = 3500
MAX_MEMORY_ITEMS = 12

# Safe default plan when the LLM step planner is unavailable.
_DETERMINISTIC_TOOLS = (
    "analytics.get_message_activity_today",
    "analytics.get_top_senders_today",
    "analytics.get_today_summary",
    "group.get_member_count",
    "group.get_moderation_status",
    "group.get_schedules",
    "audit.get_recent_actions",
)

_WRITEISH_PREFIXES = (
    "set_",
    "update_",
    "delete_",
    "purge",
    "ban",
    "mute",
    "kick",
    "warn",
    "approve",
    "reject",
    "pin",
    "unpin",
    "create_",
    "send_",
    "broadcast",
    "edit_",
    "lock",
    "unlock",
    "add_",
    "remove_",
    "clear_",
    "reset_",
    "post_",
    "enable_",
    "disable_",
)


@dataclass
class Observation:
    kind: str  # tool | code | note
    name: str
    summary: str
    data: Any = None


@dataclass
class HarnessResult:
    ok: bool
    answer: str
    steps_used: int = 0
    observations: list[Observation] = field(default_factory=list)
    error: str = ""


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…[truncated]"


def _is_auto_runnable(tool) -> bool:
    if tool.name == "harness.investigate":
        return False
    if tool.risk_level != LOW:
        return False
    if tool.requires_confirmation:
        return False
    leaf = tool.name.split(".", 1)[-1].lower()
    if any(leaf.startswith(prefix) for prefix in _WRITEISH_PREFIXES):
        return False
    return True


def _allowed_tool_names() -> list[str]:
    return [tool.name for tool in registry.all() if _is_auto_runnable(tool)]


def _format_memory(observations: list[Observation]) -> str:
    if not observations:
        return "(هنوز مشاهده‌ای ثبت نشده)"
    parts = []
    for i, obs in enumerate(observations[-MAX_MEMORY_ITEMS:], start=1):
        parts.append(f"{i}. [{obs.kind}:{obs.name}] {obs.summary}")
    return "\n".join(parts)


def _observations_payload(observations: list[Observation]) -> dict[str, Any]:
    items = []
    for obs in observations:
        items.append(
            {
                "kind": obs.kind,
                "name": obs.name,
                "summary": obs.summary,
                "data": obs.data if obs.data is not None else obs.summary,
            }
        )
    return {
        "items": items,
        "latest": items[-1] if items else None,
        "count": len(items),
    }


def _fallback_answer(observations: list[Observation]) -> str:
    if not observations:
        return "داده‌ای برای تحلیل پیدا نشد."
    lines = ["🔎 نتیجه بررسی مرحله‌ای:", ""]
    for obs in observations[-8:]:
        if obs.kind == "note":
            continue
        lines.append(f"• {obs.name}:\n{_truncate(obs.summary, 500)}")
        lines.append("")
    return "\n".join(lines).strip()


async def _run_tool(ctx, bot, tool_name: str) -> Observation:
    try:
        tool = registry.get(tool_name)
    except Exception:
        return Observation(kind="note", name="denied", summary=f"ابزار «{tool_name}» پیدا نشد.")
    if not _is_auto_runnable(tool):
        return Observation(
            kind="note",
            name="denied",
            summary=f"ابزار «{tool_name}» در harness مجاز نیست (فقط خواندنی/کم‌ریسک).",
        )
    try:
        ensure_admin_permission(ctx.admin, tool.permission)
        ensure_bot_capability(ctx.bot_capabilities, tool.capability)
    except Exception as exc:
        return Observation(kind="note", name=tool_name, summary=f"مجوز/دسترسی: {exc}")

    try:
        validated = tool.validate_params({})
        text = await tool.handler(ctx, bot, validated)
        text = _truncate(str(text or ""))
        return Observation(kind="tool", name=tool_name, summary=text, data=text)
    except Exception as exc:
        logger.exception("harness tool %s failed", tool_name)
        return Observation(kind="tool", name=tool_name, summary=f"خطا: {exc}")


def _run_code(code: str, observations: list[Observation]) -> Observation:
    payload = _observations_payload(observations)
    result = run_sandboxed(code, data=payload)
    if not result.ok:
        return Observation(kind="code", name="run_code", summary=f"SandboxError: {result.error}")
    out: dict[str, Any] = {"result": result.result}
    if result.stdout:
        out["stdout"] = result.stdout
    import json

    try:
        summary = json.dumps(out, ensure_ascii=False, default=str)
    except Exception:
        summary = str(out)
    return Observation(kind="code", name="run_code", summary=_truncate(summary), data=out)


async def _deterministic_investigation(ctx, bot, user_text: str) -> HarnessResult:
    observations: list[Observation] = []
    for name in _DETERMINISTIC_TOOLS:
        if not registry.has(name):
            continue
        observations.append(await _run_tool(ctx, bot, name))
    # Lightweight sandbox pass: count observation lengths / extract digits.
    code = (
        "nums = []\n"
        "for item in data.get('items') or []:\n"
        "    text = str(item.get('summary') or '')\n"
        "    for token in text.replace('،', ' ').split():\n"
        "        t = token.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))\n"
        "        if t.isdigit():\n"
        "            nums.append(int(t))\n"
        "result = {'observation_count': data.get('count', 0), 'numbers_seen': nums[:40]}\n"
    )
    observations.append(_run_code(code, observations))
    answer = _fallback_answer(observations)
    answer = (
        f"{answer}\n\n"
        f"——\nدرخواست: {_truncate(user_text, 200)}\n"
        "این بررسی بدون برنامه‌ریز LLM و فقط با ابزارهای خواندنی انجام شد."
    )
    return HarnessResult(
        ok=True,
        answer=answer,
        steps_used=len(observations),
        observations=observations,
        error="deterministic",
    )


@instrument_async("harness.investigation")
async def run_investigation(
    *,
    ctx,
    bot,
    user_text: str,
    max_steps: int | None = None,
    step_planner=None,
) -> HarnessResult:
    """
    Multi-step investigate → code → answer loop.

    ``step_planner`` is injectable for tests; defaults to ``ask_harness_step``.
    When AI/harness planning is disabled, a deterministic read-tool pass runs.
    """
    if not getattr(settings, "AGENT_HARNESS_ENABLED", True):
        return HarnessResult(ok=False, answer="❌ بررسی مرحله‌ای غیرفعال است.", error="harness_disabled")

    max_steps = max_steps or int(getattr(settings, "AGENT_HARNESS_MAX_STEPS", MAX_STEPS_DEFAULT))
    use_ai = bool(getattr(settings, "AGENT_AI_ENABLED", True)) and bool(
        getattr(settings, "AGENT_HARNESS_AI_ENABLED", True)
    )
    planner = step_planner or ask_harness_step

    if not use_ai and step_planner is None:
        return await _deterministic_investigation(ctx, bot, user_text)

    allowed = _allowed_tool_names()
    observations: list[Observation] = []

    for step in range(1, max_steps + 1):
        try:
            decision = await planner(
                user_text=user_text,
                memory=_format_memory(observations),
                allowed_tools=allowed,
                chat_id=ctx.chat_id,
                step=step,
                max_steps=max_steps,
            )
        except Exception:
            logger.exception("harness LLM step failed chat=%s step=%s", ctx.chat_id, step)
            if observations:
                return HarnessResult(
                    ok=True,
                    answer=_fallback_answer(observations),
                    steps_used=step,
                    observations=observations,
                    error="llm_step_failed",
                )
            # Fall back to deterministic gather if the first plan call fails.
            return await _deterministic_investigation(ctx, bot, user_text)

        thinking = (decision.get("thinking") or "").strip()
        if thinking:
            logger.info("agent_thinking chat=%s step=%s %s", ctx.chat_id, step, thinking[:240])

        action = (decision.get("action") or "").strip().lower()
        if action == "finish":
            answer = (decision.get("answer") or "").strip()
            if not answer:
                answer = _fallback_answer(observations)
            return HarnessResult(
                ok=True,
                answer=answer,
                steps_used=step,
                observations=observations,
            )

        if action == "call_tool":
            tool_name = (decision.get("tool") or "").strip()
            observations.append(await _run_tool(ctx, bot, tool_name))
            continue

        if action == "run_code":
            code = (decision.get("code") or "").strip()
            if not code:
                observations.append(Observation(kind="note", name="code", summary="کد خالی بود."))
                continue
            observations.append(_run_code(code, observations))
            continue

        observations.append(
            Observation(
                kind="note",
                name="unknown_action",
                summary=f"اقدام ناشناخته: {action!r}",
            )
        )

    return HarnessResult(
        ok=True,
        answer=_fallback_answer(observations)
        or "به سقف گام‌های بررسی رسیدم؛ داده‌های جمع‌شده را خلاصه کردم.",
        steps_used=max_steps,
        observations=observations,
        error="max_steps",
    )
