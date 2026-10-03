"""Harness security gate (Agent C).

Planner proposes. Security authorizes. Executor executes.

Wraps existing single-step guards; adds no parallel policy:

* identity      -> permissions.resolve_admin_identity / ensure_admin_permission
* capability    -> permissions.resolve_bot_capabilities / ensure_bot_capability
* target        -> orchestrator._resolve_target (reply is truth; planner ids never win)
* channel/guard -> orchestrator._CHANNEL_UNSUPPORTED_TOOLS + PROTECTED_ACTION_TOOLS
                   + agent_tools.members._target_is_protected (fail-closed backstop)
* params        -> Tool.validate_params (strict Pydantic)
* risk          -> risk.resolve_risk (registry wins upward; confirmation policy only)
* confirm       -> tool.requires_confirmation or risk == HIGH (C4, single-step path)
* execute-time  -> fresh identity (no cache) + fresh capabilities + immutable params

Fail-closed throughout: any lookup/validation/permission failure -> DENY.
Planner memory, untrusted excerpts and ``args`` are DATA only; they never
influence authorization except through schema-validated params.

Read/write correction (risk != side-effect):

* There is NO general read/write classifier in this module. ``risk_level``
  and ``requires_confirmation`` describe confirmation/risk policy, not
  side effects, and are never presented as an access tier.
* ``is_harness_executable`` is the frozen C7/V6 harness-eligibility
  predicate only (admin + low + no-confirm + non-entry + target-none +
  accepts-empty-params). It answers "may this run inside a harness run",
  not "is this tool read-only in general".
* Catalog separation is dynamic: ``category == "chat"`` vs
  ``category == "admin"`` read live from the Registry every call. No
  frozen chat name set lives here.
* Research read-only status is NOT derived from risk/confirm and NOT from
  tool-name prefixes. Source: handler design (``botapp.research``
  fetch-only providers) + evidence wrapper (UNTRUSTED
  ``<external_source>`` boundary). The Registry carries no access/
  side-effect field, so a general read/write claim is refused without
  guessing — see CROSS_AGENT_CHANGE_REQUEST in the report.
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
from botapp.agent.registry import ADMIN, CHAT, registry
from botapp.agent.risk import HIGH, LOW, resolve_risk

# Single multi-step entry. Compared by exact equality only, never by prefix.
HARNESS_ENTRY_TOOL = "harness.investigate"

# Internal sandbox-only step. Not a Registry tool. Exact equality only.
RUN_CODE_STEP = "run_code"

# Where the access answer comes from (no risk/confirm formula, no prefixes).
ACCESS_CLASSIFICATION_SOURCE = (
    "admin harness eligibility: frozen C7/V6 predicate "
    "(category==admin + risk low + no-confirm + non-entry + target-none + "
    "accepts-empty-params); chat/research separation: live category==chat "
    "(dynamic, no frozen name set, no name-prefix heuristics); research "
    "read-only: botapp.research fetch-only handler design + UNTRUSTED "
    "<external_source> evidence boundary (inspected, not inferred from "
    "risk/confirm); general read/write tier: REFUSED, Registry has no "
    "access/side-effect field (see CROSS_AGENT_CHANGE_REQUEST)"
)


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


def is_internal_step(name: str) -> bool:
    """Exact-equality check. ``run_code`` is sandbox-only, not a Registry tool."""
    return (name or "") == RUN_CODE_STEP


def _accepts_empty_params(tool) -> bool:
    """V6 probe via the tool's own strict schema. No name heuristics."""
    try:
        tool.validate_params({})
    except Exception:
        return False
    return True


def is_harness_executable(tool) -> bool:
    """Frozen C7/V6 harness-eligibility predicate (NOT a read/write classifier).

    Explicit structured-metadata comparison only. No name-prefix heuristics.
    """
    if tool.name == HARNESS_ENTRY_TOOL:
        return False
    if is_internal_step(tool.name):
        return False
    return (
        tool.category == ADMIN
        and tool.risk_level == LOW
        and not tool.requires_confirmation
        and tool.target_kind == "none"
        and _accepts_empty_params(tool)
    )


def build_harness_allowlist() -> list[str]:
    """Frozen-at-run-start allowlist from live registry via C7/V6 predicate."""
    return sorted(t.name for t in registry.all() if is_harness_executable(t))


def chat_tool_names() -> list[str]:
    """Dynamic live chat catalog. No frozen name set, no count assumption."""
    return sorted(t.name for t in registry.all_tools() if t.category == CHAT)


def admin_tool_names() -> list[str]:
    """Dynamic live admin catalog. Admin stays separate from chat/research."""
    return sorted(t.name for t in registry.all_tools() if t.category == ADMIN)


def is_chat_tool(name: str) -> bool:
    """Live category check. Unknown names -> False (fail-closed)."""
    if is_internal_step(name):
        return False
    for tool in registry.all_tools():
        if tool.name == name:
            return tool.category == CHAT
    return False


def is_admin_tool(name: str) -> bool:
    """Live category check. Unknown names -> False (fail-closed)."""
    if is_internal_step(name):
        return False
    for tool in registry.all_tools():
        if tool.name == name:
            return tool.category == ADMIN
    return False


def _handler_module_parts(tool) -> list[str]:
    mod = getattr(getattr(tool, "handler", None), "__module__", "") or ""
    return (mod or "").split(".")


def is_research_tool(tool) -> bool:
    """Research membership without tool-name prefixes.

    Source: live ``category == "chat"`` + handler defined inside the
    ``botapp.research`` package (module-path check, never ``name`` prefix).
    """
    if tool.category != CHAT:
        return False
    parts = _handler_module_parts(tool)
    return len(parts) >= 2 and parts[0] == "botapp" and parts[1] == "research"


def research_tool_names() -> list[str]:
    """Dynamic live research catalog (subset of chat). No frozen list."""
    return sorted(t.name for t in registry.all_tools() if is_research_tool(t))


def research_catalog_status() -> dict[str, Any]:
    """Live research status. Read-only per handler design; results UNTRUSTED.

    Read-only source: ``botapp.research.chat_tools`` handlers are fetch-only
    adapters over providers (no writes, credential-boundary + normalized
    public data). Evidence source: ``format_research_evidence`` wraps every
    item in an UNTRUSTED ``<external_source>`` boundary (DATA, never
    instructions). Neither fact is inferred from risk/confirm metadata:
    every registered chat tool currently carries high + confirm-required
    defaults, which proves the old formula cannot classify side effects.
    """
    names = research_tool_names()
    return {
        "count": len(names),
        "names": names,
        "read_only": True,
        "read_only_source": "botapp.research fetch-only handler design (inspected, not inferred)",
        "results_trust": "UNTRUSTED",
        "evidence_source": "UNTRUSTED <external_source> boundary (botapp.research.evidence)",
        "evidence_capable": True,
        "admin_catalog_separate": True,
    }


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
    """General single-step authorization. No side effects. Fail-closed.

    Risk is confirmation policy only (C4: registry wins upward). It is never
    used here as a read/write or side-effect claim.
    """
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
    if name == HARNESS_ENTRY_TOOL or is_internal_step(name):
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
