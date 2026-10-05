"""Admin agent orchestrator.

Implements the full pipeline:

trigger → admin verification → deterministic parse → (AI parse) → schema
validation → registry lookup → app permission → bot capability → target
resolution → risk classification → confirmation (if needed) → existing-service
execution → audit → Persian response.

The AI model never executes anything: it can only produce a validated,
allowlisted decision that this module then routes through existing services.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.conf import settings as dj_settings

logger = logging.getLogger("botapp.agent")

from botapp.models import AgentConfirmation

from . import confirmations
from .audit_log import record_audit
from .context import AgentContext, reply_target_from_message
from .errors import (
    AgentError,
    AgentParseError,
    AmbiguousTarget,
    ProtectedTarget,
)
from .permissions import (
    ensure_admin_permission,
    ensure_bot_capability,
    resolve_admin_identity,
    resolve_bot_capabilities,
)
from .parser import parse as deterministic_parse
from .registry import registry
from .responses import confirmation_preview
from .risk import HIGH, resolve_risk
from .schemas import AgentDecision
from .target_chat import resolve_target_chat

PROTECTED_ACTION_TOOLS = {"member.mute", "member.ban", "member.warn"}
# These rely on group-member restriction APIs and are not valid in channels.
_CHANNEL_UNSUPPORTED_TOOLS = frozenset({
    "member.mute",
    "member.unmute",
    "member.ban",
    "member.unban",
    "member.warn",
    "member.get_warnings",
    "group.lock",
    "group.unlock",
})


@dataclass
class AgentResult:
    text: str
    confirmation: AgentConfirmation | None = None
    confirm_token: str | None = None
    error: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return self.confirmation is not None and self.confirm_token is not None


def _setting(name: str, default):
    return getattr(dj_settings, name, default)


@sync_to_async(thread_sensitive=True)
def _get_group_settings(chat_id: int):
    from botapp.models import GroupSettings

    group, _ = GroupSettings.objects.get_or_create(chat_id=chat_id)
    return group


async def _build_context(bot, message, command_text: str) -> AgentContext:
    try:
        target = await resolve_target_chat(bot, message, command_text)
    except ValueError as exc:
        raise AgentParseError(str(exc)) from exc

    identity = await resolve_admin_identity(bot, target.chat_id, message.from_user)
    capabilities = await resolve_bot_capabilities(bot, target.chat_id)
    group = await _get_group_settings(target.chat_id)
    return AgentContext(
        chat_id=int(target.chat_id),
        chat_type=target.chat_type or str(message.chat.type),
        chat_title=target.title or getattr(message.chat, "title", "") or "",
        admin=identity,
        bot_capabilities=capabilities,
        command_text=target.cleaned_command,
        reply=reply_target_from_message(message),
        group_settings=group,
        timezone_name=_setting("TIME_ZONE", ""),
        request_chat_id=int(message.chat.id),
        target_source=target.source,
    )


async def _decide(ctx: AgentContext, command_text: str, ai_provider) -> tuple[AgentDecision, str]:
    decision = deterministic_parse(command_text)
    if decision is not None:
        return decision, "deterministic"

    if not _setting("AGENT_AI_ENABLED", True) or ai_provider is None:
        raise AgentParseError()

    min_conf = float(_setting("AGENT_MIN_CONFIDENCE", 0.80))
    decision = await ai_provider.parse_admin_command(
        command_text, ctx, registry.ai_catalog()
    )
    if not registry.has(decision.tool):
        raise AgentParseError(
            "❌ از دستور مطمئن نیستم. لطفاً واضح‌تر و مشخص‌تر بنویسید."
        )
    if decision.confidence < min_conf:
        if (
            _setting("AGENT_HARNESS_ENABLED", True)
            and decision.confidence >= 0.5
            and registry.has("harness.investigate")
        ):
            return (
                AgentDecision(
                    intent="investigate",
                    tool="harness.investigate",
                    confidence=decision.confidence,
                    risk_level="low",
                    requires_confirmation=False,
                    human_summary="بررسی مرحله‌ای",
                ),
                "ai-harness",
            )
        raise AgentParseError(
            "❌ از دستور مطمئن نیستم. لطفاً واضح‌تر و مشخص‌تر بنویسید."
        )
    return decision, "ai"


def _resolve_target(tool, ctx: AgentContext, params: dict) -> dict:
    params = dict(params or {})
    if tool.target_kind == "member":
        if ctx.reply and ctx.reply.user_id:
            # Reply is the single source of truth; never trust an AI-provided id.
            params["target_user_id"] = int(ctx.reply.user_id)
        elif params.get("target_user_id") is None:
            raise AmbiguousTarget(
                "❌ کاربر هدف مشخص نیست. برای اجرای امن، روی یکی از پیام‌های او Reply کنید."
            )
    elif tool.target_kind == "message":
        if ctx.reply and ctx.reply.message_id:
            params["message_id"] = int(ctx.reply.message_id)
        elif params.get("message_id") is None:
            raise AmbiguousTarget("❌ لطفاً روی پیام موردنظر Reply کنید.")
    return params


async def _ensure_target_allowed(bot, ctx: AgentContext, tool, target_user_id) -> None:
    if tool.name not in PROTECTED_ACTION_TOOLS or target_user_id is None:
        return
    try:
        me = await bot.me()
        if int(target_user_id) == int(me.id):
            raise ProtectedTarget("❌ نمی‌توان این عملیات را روی خود ربات انجام داد.")
        member = await bot.get_chat_member(ctx.chat_id, int(target_user_id))
    except ProtectedTarget:
        raise
    except Exception:
        return  # capability guard / pipeline will still protect us
    if getattr(member, "status", None) in {"creator", "administrator"}:
        raise ProtectedTarget("❌ کاربر هدف ادمین گروه است و نمی‌توان این عملیات را روی او انجام داد.")


def _target_label(ctx: AgentContext) -> str:
    if ctx.reply:
        return ctx.reply.full_name or (f"@{ctx.reply.username}" if ctx.reply.username else "")
    return ""


async def handle_admin_command(bot, message, command_text: str, *, ai_provider=None) -> AgentResult:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    request_chat_id = int(message.chat.id)
    user_id = int(message.from_user.id)
    command_text = (command_text or "").strip()

    max_len = int(_setting("AGENT_MAX_COMMAND_LENGTH", 2000))
    if len(command_text) > max_len:
        command_text = command_text[:max_len]

    # Operational chat_id is filled after context resolution (may be remote).
    state = {"audit_chat_id": request_chat_id, "command_text": command_text}

    async def _audit(**kwargs):
        await sync_to_async(record_audit, thread_sensitive=True)(
            request_id=request_id,
            chat_id=state["audit_chat_id"],
            requester_user_id=user_id,
            original_command=state["command_text"],
            duration_ms=int((time.perf_counter() - started) * 1000),
            **kwargs,
        )

    try:
        ctx = await _build_context(bot, message, command_text)
        state["audit_chat_id"] = ctx.chat_id
        command_text = ctx.command_text or command_text
        state["command_text"] = command_text
        if not ctx.admin.is_admin:
            from .errors import AgentPermissionDenied

            raise AgentPermissionDenied()

        from .parser import is_message_analysis_request

        if is_message_analysis_request(command_text):
            raise AgentParseError(
                "❌ این درخواست مربوط به تحلیل متن است و عملیات مدیریتی اجرا نمی‌کند."
            )

        decision, parser_type = await _decide(ctx, command_text, ai_provider)
        tool = registry.get(decision.tool)

        if ctx.is_channel and tool.name in _CHANNEL_UNSUPPORTED_TOOLS:
            raise AgentParseError(
                "❌ این عملیات مخصوص گروه است و در کانال پشتیبانی نمی‌شود.\n"
                "برای کانال از ابزارهای اطلاعات/آمار/پست/حذف/پین استفاده کنید."
            )

        raw_params = decision.parameters.model_dump()
        raw_params = _resolve_target(tool, ctx, raw_params)
        # Drop unset (None) values so each tool's strict schema applies its own
        # defaults instead of receiving nulls it does not accept.
        raw_params = {key: value for key, value in raw_params.items() if value is not None}
        validated = tool.validate_params(raw_params)

        ensure_admin_permission(ctx.admin, tool.permission)
        ensure_bot_capability(ctx.bot_capabilities, tool.capability)
        await _ensure_target_allowed(bot, ctx, tool, raw_params.get("target_user_id"))

        risk = resolve_risk(tool.risk_level, decision.risk_level)
        requires_confirmation = tool.requires_confirmation or risk == HIGH

        if requires_confirmation:
            confirmation, raw_token = await sync_to_async(
                confirmations.create_confirmation, thread_sensitive=True
            )(
                chat_id=ctx.chat_id,
                request_chat_id=ctx.request_chat_id or request_chat_id,
                requester_user_id=user_id,
                requester_name=ctx.admin.display_name,
                tool_name=tool.name,
                validated_parameters=validated.model_dump(),
                human_summary=decision.human_summary or tool.human_verb,
                risk_level=risk,
                target_user_id=raw_params.get("target_user_id"),
                request_message_id=int(message.message_id),
                ttl_seconds=int(_setting("AGENT_CONFIRMATION_TTL_SECONDS", 120)),
            )
            preview = confirmation_preview(
                verb=tool.human_verb or decision.human_summary or tool.name,
                target_label=_target_label(ctx),
                target_user_id=raw_params.get("target_user_id"),
                duration_minutes=validated.model_dump().get("duration_minutes"),
                delay_minutes=validated.model_dump().get("delay_minutes", 0) or 0,
                reason=validated.model_dump().get("reason", "") or "",
                value=validated.model_dump().get("value"),
                requester_name=ctx.admin.display_name,
                group_name=ctx.chat_title,
                risk_level=risk,
            )
            await _audit(
                requester_role=ctx.admin.role,
                parser_type=parser_type,
                detected_intent=decision.intent,
                tool_name=tool.name,
                parameters=validated.model_dump(),
                risk_level=risk,
                confirmation_status="pending",
                execution_status="awaiting_confirmation",
            )
            return AgentResult(text=preview, confirmation=confirmation, confirm_token=raw_token)

        # Trail the intent before side effects so a crash mid-handler still leaves
        # an audit row (confirmation path already claims the row first).
        await _audit(
            requester_role=ctx.admin.role,
            parser_type=parser_type,
            detected_intent=decision.intent,
            tool_name=tool.name,
            parameters=validated.model_dump(),
            risk_level=risk,
            confirmation_status="not_required",
            execution_status="started",
        )
        result_text = await tool.handler(ctx, bot, validated)
        await _audit(
            requester_role=ctx.admin.role,
            parser_type=parser_type,
            detected_intent=decision.intent,
            tool_name=tool.name,
            parameters=validated.model_dump(),
            risk_level=risk,
            confirmation_status="not_required",
            execution_status="executed",
            executed=True,
        )
        return AgentResult(text=result_text)

    except AgentError as exc:
        await _audit(execution_status="failed", error_code=exc.code)
        return AgentResult(text=exc.user_message, error=True)
    except Exception:  # defensive: never leak internals to the user
        # Log with a full stack trace + request_id for debugging; the user only
        # ever sees a generic message and no technical detail is surfaced.
        logger.exception(
            "agent command failed request_id=%s chat=%s",
            request_id,
            state.get("audit_chat_id", request_chat_id),
        )
        await _audit(execution_status="failed", error_code="unexpected_error")
        return AgentResult(text="❌ خطای غیرمنتظره‌ای رخ داد.", error=True)


async def execute_confirmed(bot, confirmation: AgentConfirmation) -> str:
    """Execute a confirmation that has already been claimed (status=executing).

    Re-checks permission and capability, then runs the same tool handler with the
    immutable stored parameters.
    """
    started = time.perf_counter()
    request_id = uuid.uuid4().hex

    async def _audit(status: str, error_code: str = "", executed: bool = False):
        await sync_to_async(record_audit, thread_sensitive=True)(
            request_id=request_id,
            chat_id=confirmation.chat_id,
            requester_user_id=confirmation.requester_user_id,
            requester_role="",
            original_command=confirmation.human_summary,
            parser_type="confirmed",
            detected_intent=confirmation.tool_name,
            tool_name=confirmation.tool_name,
            parameters=confirmation.validated_parameters,
            risk_level=confirmation.risk_level,
            confirmation_status="confirmed",
            execution_status=status,
            error_code=error_code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            executed=executed,
        )

    try:
        # Look up the tool inside the try so a registry change (e.g. a tool
        # removed between confirmation and execution) fails gracefully instead
        # of leaving the row stuck.
        tool = registry.get(confirmation.tool_name)
        identity = await resolve_admin_identity(
            bot,
            confirmation.chat_id,
            type("U", (), {"id": confirmation.requester_user_id, "full_name": confirmation.requester_name})(),
            use_cache=False,
        )
        ensure_admin_permission(identity, tool.permission)
        capabilities = await resolve_bot_capabilities(bot, confirmation.chat_id)
        ensure_bot_capability(capabilities, tool.capability)

        group = await _get_group_settings(confirmation.chat_id)
        ctx = AgentContext(
            chat_id=confirmation.chat_id,
            chat_type="supergroup",
            chat_title=group.chat_title if group else "",
            admin=identity,
            bot_capabilities=capabilities,
            group_settings=group,
            timezone_name=_setting("TIME_ZONE", ""),
        )
        validated = tool.validate_params(confirmation.validated_parameters)
        await _audit("started")
        result_text = await tool.handler(ctx, bot, validated)

        await sync_to_async(confirmations.mark_executed, thread_sensitive=True)(confirmation, result_text)
        await _audit("executed", executed=True)
        return result_text
    except AgentError as exc:
        await sync_to_async(confirmations.mark_failed, thread_sensitive=True)(confirmation, exc.code)
        await _audit("failed", error_code=exc.code)
        return exc.user_message
    except Exception:
        logger.exception(
            "agent confirmation execution failed request_id=%s confirmation=%s tool=%s",
            request_id,
            confirmation.pk,
            confirmation.tool_name,
        )
        await sync_to_async(confirmations.mark_failed, thread_sensitive=True)(confirmation, "unexpected_error")
        await _audit("failed", error_code="unexpected_error")
        return "❌ اجرای عملیات ناموفق بود."
