"""Request-local lifecycle for Noya's one-minute waiting notice.

The wait reply is an actual Telegram message, never part of the generated
answer. Its ID stays inside the current asyncio request context. After a
successful final Telegram send, delete precisely that wait reply. A failed
final send must never prematurely remove the only visible status.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitingNotice:
    chat_id: int
    request_message_id: int
    notice_message_id: int


_PENDING_NOTICE: ContextVar[WaitingNotice | None] = ContextVar(
    "noya_pending_waiting_notice", default=None,
)


def remember_waiting_notice(message, sent_notice) -> None:
    """Remember only the Telegram message returned from this request's reply."""
    chat = getattr(message, "chat", None)
    sent_chat = getattr(sent_notice, "chat", None)
    try:
        chat_id = int(getattr(chat, "id", 0) or 0)
        request_id = int(getattr(message, "message_id", 0) or 0)
        notice_id = int(getattr(sent_notice, "message_id", 0) or 0)
        sent_chat_id = int(getattr(sent_chat, "id", chat_id) or 0)
    except (TypeError, ValueError):
        return
    if not chat_id or request_id <= 0 or notice_id <= 0 or sent_chat_id != chat_id:
        return
    _PENDING_NOTICE.set(WaitingNotice(
        chat_id=chat_id,
        request_message_id=request_id,
        notice_message_id=notice_id,
    ))


async def clear_waiting_notice_after_send(message) -> bool:
    """Best-effort cleanup after *confirmed* final send, never before.

    The trigger message ID protects against reuse by a later request in the
    same task. Clearing the ContextVar before awaiting Telegram makes duplicate
    deletion impossible even with nested delivery paths.
    """
    pending = _PENDING_NOTICE.get()
    if pending is None:
        return False
    try:
        chat_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
        request_id = int(getattr(message, "message_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    if (chat_id != pending.chat_id
            or request_id != pending.request_message_id):
        return False

    _PENDING_NOTICE.set(None)
    try:
        await message.bot.delete_message(
            chat_id=pending.chat_id,
            message_id=pending.notice_message_id,
        )
        logger.info(
            "[NOYA-WAIT] cleanup=ok chat=%s request=%s notice=%s",
            pending.chat_id, pending.request_message_id,
            pending.notice_message_id,
        )
        return True
    except Exception as exc:
        # Telegram may disallow deleting the bot's old message, the message
        # may already be gone, or the connection may fail. The real answer
        # was successfully sent; its delivery must not be rolled back.
        logger.info(
            "[NOYA-WAIT] cleanup=failed chat=%s request=%s type=%s",
            pending.chat_id, pending.request_message_id,
            type(exc).__name__,
        )
        return False
