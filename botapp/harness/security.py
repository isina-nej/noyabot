"""Harness security gate (Agent C).

Planner proposes. Security authorizes. Executor executes.

Wraps existing single-step guards; adds no parallel policy:

* identity      -> permissions.resolve_admin_identity / ensure_admin_permission
* capability    -> permissions.resolve_bot_capabilities / ensure_bot_capability
* target        -> orchestrator._resolve_target (reply is truth; planner ids never win)
* channel/guard -> orchestrator._CHANNEL_UNSUPPORTED_TOOLS + PROTECTED_ACTION_TOOLS
                   + agent_tools.members._target_is_protected (fail-closed backstop)
* params        -> Tool.validate_params (strict Pydantic)
* risk          -> risk.resolve_risk (registry wins upward)
* confirm       -> tool.requires_confirmation or risk == HIGH
* execute-time  -> fresh identity (no cache) + fresh capabilities + immutable params

Fail-closed throughout: any lookup/validation/permission failure -> DENY.
Planner memory, untrusted excerpts and ``args`` are DATA only; they never
influence authorization except through schema-validated params.

Harness predicate uses structured Registry metadata only
(category/risk_level/requires_confirmation/name). No name-prefix heuristics.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from botapp.agent.errors import AgentError
from botapp.agent.orchestrator import (
    PROTECTED_ACTION_TOOLS,
    _CHANNEL_UNSUPPORTED_TOOLS,
    _ensure_target_allowed,
    _resolve_target,
)
from botapp.agent.permissions import (
    ensure_admin_permission,
    ensure_bot_capability,
    resolve_admin_identity,
    resolve_bot_capabilities,
)
from botapp.agent.registry import registry
from botapp.agent.risk import HIGH, LOW, resolve_risk


class AuthVerdict(str, Enum):
    ALLOW = "allow"
    NEED_CONFIRMATION = "need_confirmation"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class AuthDecision:
    verdict: AuthVerdict
    tool_name: str
    risk: str
    requires_confirmation: bool
    validated_params: dict[str, Any] | None
    reason_code: str
    user_message: str


def _deny(tool_name: str, risk: str, code: str, msg: str) -> AuthDecision:
    return AuthDecision(
        verdict=AuthVerdict.DENY,
        tool_name=tool_name,
        risk=risk,
        requires_confirmation=False,
        validated_params=None,
        reason_code=code,
        user_message=msg,
    )


def is_harness_executable(tool) -> bool:
    """C7 predicate over structured metadata. No prefix matching."""
    if tool.name == "harness.investigate":
        return False
    return (
        tool.category == "admin"
        and tool.risk_level == LOW
        and not tool.requires_confirmation
    )


def build_harness_allowlist() -> list[str]:
    """Frozen-at-run-start allowlist from live registry via C7 predicate."""
    return [t.name for t in registry.all() if is_harness_executable(t)]


async def _protected_check(bot, ctx, tool, target_user_id: int | None) -> None:
    """Orchestrator pre-check (preserves semantics) + fail-closed backstop."""
    if tool.name not in PROTECTED_ACTION_TOOLS or target_user_id is None:
        return
    if bot is None:
        from botapp.agent.errors import ProtectedTarget

        raise ProtectedTarget("❌ نمی‌توان این عملیات را روی این کاربر انجام داد.")
    await _ensure_target_allowed(bot, ctx, tool, target_user_id)
    try:
        from botapp.agent_tools.members import _target_is_protected
    except Exception:
        return
    if await _target_is_protected(ctx, bot, int(target_user_id)):
        from botapp.agent.errors import ProtectedTarget

        raise ProtectedTarget(
            "❌ کاربر هدف ادمین گروه است و نمی‌توان این عملیات را روی او انجام داد."
        )


async def authorize(
    ctx,
    tool_name: str,
    raw_params: dict[str, Any] | None,
    *,
    ai_risk: str | None = None,
    bot=None,
) -> AuthDecision:
    """General single-step authorization. No side effects. Fail-closed."""
    try:
        tool = registry.get((tool_name or "").strip())
    except AgentError as exc:
        return _deny(tool_name or "", LOW, exc.code, exc.user_message)
    except Exception:
        return _deny(tool_name or "", LOW, "unknown_tool", "❌ این عملیات پشتیبانی نمی‌شود.")

    if ctx.is_channel and tool.name in _CHANNEL_UNSUPPORTED_TOOLS:
        return _deny(
            tool.name,
            tool.risk_level,
            "parse_error",
            "❌ این عملیات مخصوص گروه است و در کانال پشتیبانی نمی‌شود.\n"
            "برای کانال از ابزارهای اطلاعات/آمار/پست/حذف/پین استفاده کنید.",
        )

    try:
        resolved = _resolve_target(tool, ctx, dict(raw_params or {}))
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)
    resolved = {k: v for k, v in resolved.items() if v is not None}

    try:
        validated = tool.validate_params(resolved)
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)

    try:
        ensure_admin_permission(ctx.admin, tool.permission)
        ensure_bot_capability(ctx.bot_capabilities, tool.capability)
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)

    try:
        await _protected_check(bot, ctx, tool, resolved.get("target_user_id"))
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)

    risk = resolve_risk(tool.risk_level, ai_risk)
    need = bool(tool.requires_confirmation or risk == HIGH)
    if need:
        return AuthDecision(
            verdict=AuthVerdict.NEED_CONFIRMATION,
            tool_name=tool.name,
            risk=risk,
            requires_confirmation=True,
            validated_params=validated.model_dump(),
            reason_code="confirmation_required",
            user_message="⚠️ این عملیات نیاز به تأیید دارد.",
        )
    return AuthDecision(
        verdict=AuthVerdict.ALLOW,
        tool_name=tool.name,
        risk=risk,
        requires_confirmation=False,
        validated_params=validated.model_dump(),
        reason_code="",
        user_message="",
    )


async def authorize_harness_step(
    ctx,
    tool_name: str,
    *,
    bot=None,
    allowlist: list[str] | None = None,
) -> AuthDecision:
    """Harness step authorization: C7 + V4/V5/V6. NEED_CONFIRMATION -> DENY."""
    name = (tool_name or "").strip()
    if name == "harness.investigate":
        return _deny(name, LOW, "denied", "ابزار «harness.investigate» در harness مجاز نیست.")
    if allowlist is not None and name not in allowlist:
        return _deny(name, LOW, "denied", f"ابزار «{name}» در فهرست مجاز این اجرا نیست.")
    try:
        tool = registry.get(name)
    except AgentError as exc:
        return _deny(name, LOW, exc.code, exc.user_message)
    if not is_harness_executable(tool):
        return _deny(
            name,
            tool.risk_level,
            "denied",
            f"ابزار «{name}» در harness مجاز نیست (فقط خواندنی/کم‌ریسک).",
        )
    if tool.target_kind != "none":
        # Phase 1 runs tools with {}; target-bearing tools fail V6 by construction.
        return _deny(
            name, tool.risk_level, "denied", f"ابزار «{name}» هدف‌دار است و در harness اجرا نمی‌شود."
        )
    decision = await authorize(ctx, name, {}, bot=bot)
    if decision.verdict == AuthVerdict.NEED_CONFIRMATION:
        return AuthDecision(
            verdict=AuthVerdict.DENY,
            tool_name=decision.tool_name,
            risk=decision.risk,
            requires_confirmation=False,
            validated_params=None,
            reason_code="denied",
            user_message=f"ابزار «{name}» نیاز به تأیید دارد و در harness مجاز نیست.",
        )
    return decision


async def revalidate_for_execution(
    bot,
    *,
    chat_id: int,
    requester_user_id: int,
    requester_name: str = "",
    tool_name: str,
    stored_params: dict[str, Any] | None,
    ctx=None,
) -> AuthDecision:
    """Execute-time revalidation: fresh identity (no cache) + immutable params."""
    try:
        tool = registry.get((tool_name or "").strip())
    except AgentError as exc:
        return _deny(tool_name or "", LOW, exc.code, exc.user_message)
    try:
        validated = tool.validate_params(dict(stored_params or {}))
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)
    try:
        ident_user = type(
            "U", (), {"id": int(requester_user_id), "full_name": requester_name or str(requester_user_id)}
        )()
        identity = await resolve_admin_identity(bot, int(chat_id), ident_user, use_cache=False)
        ensure_admin_permission(identity, tool.permission)
        capabilities = await resolve_bot_capabilities(bot, int(chat_id))
        ensure_bot_capability(capabilities, tool.capability)
    except AgentError as exc:
        return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)
    except Exception:
        return _deny(tool.name, tool.risk_level, "permission_denied", "❌ مجوز اجرای عملیات تأیید نشد.")
    if ctx is not None and ctx.is_channel and tool.name in _CHANNEL_UNSUPPORTED_TOOLS:
        return _deny(tool.name, tool.risk_level, "parse_error", "❌ این عملیات در کانال پشتیبانی نمی‌شود.")
    target_uid = (validated.model_dump() or {}).get("target_user_id")
    if ctx is not None:
        try:
            await _protected_check(bot, ctx, tool, target_uid)
        except AgentError as exc:
            return _deny(tool.name, tool.risk_level, exc.code, exc.user_message)
    risk = resolve_risk(tool.risk_level, None)
    return AuthDecision(
        verdict=AuthVerdict.ALLOW,
        tool_name=tool.name,
        risk=risk,
        requires_confirmation=False,
        validated_params=validated.model_dump(),
        reason_code="",
        user_message="",
    )
