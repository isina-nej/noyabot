"""aiogram wiring for the admin agent.

This module is additive: it exposes an ``agent_router`` and an
``ArchiveMiddleware`` that ``build_dispatcher`` includes alongside the existing
routers. Existing handlers are untouched.

Routing safety:
* ``/agent`` and ``/adminai`` are dedicated commands with zero overlap with
  existing handlers.
* The natural-language "نویا،" / reply-to-bot path classifies with AI
  (fail-open to Noya chat). Known deterministic admin commands skip the
  classifier. Non-admins never trigger the agent.
"""

from __future__ import annotations

import logging

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from asgiref.sync import sync_to_async
from django.conf import settings as dj_settings

from botapp.agent import callbacks, confirmations
from botapp.agent.ai import NoyaAgentProvider
from botapp.agent.orchestrator import AgentResult, handle_admin_command
from botapp.agent.classify import should_route_to_agent
from botapp.agent.permissions import resolve_admin_identity
from botapp import activity
from botapp import agent_tools  # noqa: F401  (registers tools into the registry)
from botapp import message_archive
from botapp.noya_context import remember_group_message

logger = logging.getLogger("botapp.agent")

router = Router(name="agent")

# Explicit admin trigger: ALWAYS routes an admin's message to the agent
# (deterministic parser first, then AI). Checked before the plain trigger
# because "نویا مدیر، …" also starts with the plain "نویا " prefix.
_ADMIN_PREFIXES = ("نویا مدیر،", "نویا مدیر ,", "نویا مدیر,", "نویا مدیر ", "noya admin,", "noya admin ")
# Plain trigger: only routes to the agent when the deterministic parser already
# recognises a definite admin intent; otherwise the message falls through to
# normal Noya chat unchanged.
_NOYA_PREFIXES = ("نویا،", "نویا ,", "نویا,", "نویا ", "noya,", "noya ")


def _agent_enabled() -> bool:
    return bool(getattr(dj_settings, "AGENT_ENABLED", True))


def _ai_provider():
    if getattr(dj_settings, "AGENT_AI_ENABLED", True):
        return NoyaAgentProvider()
    return None


def _strip_prefix(stripped: str, lowered: str, prefixes) -> str | None:
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


class AgentTriggerFilter(BaseFilter):
    """Route admin natural-language commands to the agent.

    * "نویا مدیر، <command>" (admin only) → ALWAYS routed to the agent, so
      complex commands that need the AI parser are not lost to Noya chat.
    * "نویا، <command>" (admin only) → routed only when the deterministic parser
      recognises a definite intent; anything else falls through to Noya chat,
      preserving existing behaviour.

    Non-admins never trigger either form. The admin check runs after the cheap
    prefix/parse checks so normal chat traffic does not incur an API call.
    """

    async def __call__(self, message: Message, bot) -> bool | dict:
        if not _agent_enabled():
            return False
        # Groups/supergroups: in-place admin commands.
        # Private: remote manage/analyze of a named or forwarded target chat.
        # Channels: rarely interactive; still allow if an admin posts a command.
        if not message.text or message.chat.type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
            ChatType.PRIVATE,
            ChatType.CHANNEL,
        }:
            return False
        stripped = message.text.lstrip()
        lowered = stripped.casefold()

        # 1) Explicit admin trigger — force the agent (deterministic then AI).
        admin_command = _strip_prefix(stripped, lowered, _ADMIN_PREFIXES)
        if admin_command is not None:
            if not admin_command.strip():
                return False
            # Cheap gate: in private, ADMIN_IDS or later target-chat admin check
            # inside the orchestrator is authoritative. Here we only require the
            # sender to look like an admin of *this* chat when not private.
            if message.chat.type != ChatType.PRIVATE:
                identity = await resolve_admin_identity(bot, message.chat.id, message.from_user)
                if not identity.is_admin:
                    return False
            else:
                identity = await resolve_admin_identity(bot, message.chat.id, message.from_user)
                if not identity.is_admin:
                    # Still allow private if the command embeds a target; the
                    # orchestrator will verify adminship on the resolved chat.
                    from botapp.agent.target_chat import extract_explicit_chat_ref

                    ref, _ = extract_explicit_chat_ref(admin_command)
                    if ref is None and not getattr(message, "reply_to_message", None):
                        return False
            return {"agent_command": admin_command}

        # 2) Plain "نویا، …" — deterministic ops, else AI classifies agent vs chat.
        command = _strip_prefix(stripped, lowered, _NOYA_PREFIXES)
        if command:
            from botapp.agent.target_chat import extract_explicit_chat_ref

            _, remainder = extract_explicit_chat_ref(command)
            parse_text = remainder or command
            if message.chat.type != ChatType.PRIVATE:
                identity = await resolve_admin_identity(bot, message.chat.id, message.from_user)
                if not identity.is_admin:
                    return False
            if not await should_route_to_agent(parse_text, chat_id=int(message.chat.id)):
                return False
            return {"agent_command": command}

        # 3) Admin reply-to-bot: AI classifies (fail-open to Noya chat).
        replied = getattr(message, "reply_to_message", None)
        replied_user = getattr(replied, "from_user", None) if replied else None
        if replied_user is not None and message.chat.type in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
            ChatType.PRIVATE,
        }:
            me = await bot.me()
            if int(replied_user.id) == int(me.id):
                # Don't route obvious casual/clock/search/image requests to agent
                if any(w in stripped for w in ("عکس", "تصویر", "نقاشی", "draw", "image", "بکش", "بساز")):
                    return False

                from botapp.agent.target_chat import extract_explicit_chat_ref
                from botapp.noya_clock import is_clock_question
                from botapp.noya_search import needs_web_search

                if is_clock_question(stripped) or needs_web_search(stripped):
                    return False

                _, remainder = extract_explicit_chat_ref(stripped)
                parse_text = remainder or stripped
                identity = await resolve_admin_identity(
                    bot, message.chat.id, message.from_user
                )
                if identity.is_admin and await should_route_to_agent(
                    parse_text, chat_id=int(message.chat.id)
                ):
                    return {"agent_command": stripped}
        return False


def _confirmation_keyboard(raw_token: str) -> InlineKeyboardMarkup:
    # The RAW opaque token travels only in callback_data; the DB stores only its
    # SHA-256 hash. prefix (<=14 bytes) + token_urlsafe(24) (32 chars) stays well
    # under Telegram's 64-byte callback_data limit.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید", callback_data=f"{confirmations.CONFIRM_PREFIX}{raw_token}"),
                InlineKeyboardButton(text="❌ لغو", callback_data=f"{confirmations.CANCEL_PREFIX}{raw_token}"),
            ]
        ]
    )


@sync_to_async(thread_sensitive=True)
def _store_confirmation_message_id(confirmation_id: int, message_id: int) -> None:
    from botapp.models import AgentConfirmation

    AgentConfirmation.objects.filter(pk=confirmation_id).update(confirmation_message_id=message_id)


async def _dispatch(message: Message, command: str) -> None:
    if not _agent_enabled():
        await message.reply("دستیار مدیریتی غیرفعال است.")
        return
    if not command.strip():
        await message.reply(
            "استفاده: دستور مدیریتی را بعد از /agent بنویسید.\n\n"
            "در گروه:\n"
            "/agent تعداد اعضا چقدره؟\n"
            "/agent تحلیل کن\n\n"
            "از پیوی برای کانال/گروه دیگر:\n"
            "/agent @mychannel آمار امروز\n"
            "/agent @mychannel تحلیل کن\n"
            "/agent -100123456789 لیست ادمین‌ها"
        )
        return

    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass
    result: AgentResult = await handle_admin_command(
        message.bot, message, command, ai_provider=_ai_provider()
    )
    if result.needs_confirmation:
        sent = await message.reply(result.text, reply_markup=_confirmation_keyboard(result.confirm_token))
        await _store_confirmation_message_id(result.confirmation.id, sent.message_id)
        return
    await message.reply(result.text)


@router.message(Command("agent"))
async def agent_command(message: Message, command: CommandObject):
    await _dispatch(message, (command.args or "").strip())


@router.message(Command("adminai"))
async def adminai_command(message: Message, command: CommandObject):
    await _dispatch(message, (command.args or "").strip())


@router.message(AgentTriggerFilter())
async def agent_natural_trigger(message: Message, agent_command: str):
    await _dispatch(message, agent_command)


async def _handle_callback(callback: CallbackQuery, action: str, raw_token: str) -> None:
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id if callback.message else None
    if chat_id is None:
        await callback.answer()
        return
    if action == "confirm":
        result = await callbacks.process_confirm(callback.bot, raw_token, user_id=user_id, chat_id=chat_id)
    else:
        result = await callbacks.process_cancel(callback.bot, raw_token, user_id=user_id, chat_id=chat_id)
    await callback.answer()
    try:
        await callback.message.edit_text(result.text)
    except Exception:  # message may be too old / identical; fall back to a reply
        await callback.message.answer(result.text)


@router.callback_query(F.data.startswith(confirmations.CONFIRM_PREFIX))
async def confirm_callback(callback: CallbackQuery):
    await _handle_callback(callback, "confirm", callback.data[len(confirmations.CONFIRM_PREFIX):])


@router.callback_query(F.data.startswith(confirmations.CANCEL_PREFIX))
async def cancel_callback(callback: CallbackQuery):
    await _handle_callback(callback, "cancel", callback.data[len(confirmations.CANCEL_PREFIX):])


class ArchiveMiddleware(BaseMiddleware):
    """Observe every chat message for analytics; optionally archive full text.

    * Activity counters (message/media/unique senders per day) are ALWAYS on.
    * Full ``MessageSnapshot`` archival remains opt-in via ``MESSAGE_ARCHIVE_ENABLED``.

    Never blocks or alters the handler pipeline.
    """

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            try:
                await sync_to_async(activity.record_message_activity, thread_sensitive=True)(event)
            except Exception:  # activity must never break message handling
                logger.debug("activity counter failed", exc_info=True)
            if message_archive.archive_enabled():
                try:
                    await sync_to_async(message_archive.archive_message, thread_sensitive=True)(event)
                except Exception:  # archival must never break message handling
                    logger.debug("message archival failed", exc_info=True)
        # Run handlers first so Noya's "recent chat" buffer excludes the current ask.
        result = await handler(event, data)
        if isinstance(event, Message):
            try:
                remember_group_message(event)
            except Exception:  # context buffer must never break message handling
                logger.debug("noya recent buffer failed", exc_info=True)
        return result
