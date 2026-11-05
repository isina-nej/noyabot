"""Request-scoped, best-effort Telegram chat-action heartbeat for Noya.

Telegram expires chat actions in ~5 seconds. Keep an action refreshed every
4 seconds until the reply handler returns. Never let status failures prevent
an actual answer. A ContextVar separates concurrent chats and bot identities.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

logger = logging.getLogger(__name__)

_REFRESH_SECONDS = 4.0
_ACTIVE_ACTION: ContextVar["NoyaChatAction | None"] = ContextVar(
    "noya_active_chat_action", default=None,
)
_VALID_ACTIONS = frozenset({"typing", "record_voice", "upload_voice", "upload_photo"})


class NoyaChatAction:
    """One status sender owned by one bot/chat/topic request."""

    def __init__(self, message, *, action: str = "typing", interval: float = _REFRESH_SECONDS):
        if action not in _VALID_ACTIONS:
            raise ValueError("Unsupported Telegram chat action")
        self.bot = getattr(message, "bot", None)
        self.chat_id = getattr(message.chat, "id", None)
        self.message_thread_id = getattr(message, "message_thread_id", None)
        self.action = action
        self.interval = interval
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _send(self):
        if self.bot is None or self.chat_id is None:
            return
        kwargs = {"chat_id": self.chat_id, "action": self.action}
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        try:
            await self.bot.send_chat_action(**kwargs)
        except Exception as exc:
            # A forbidden chat action, slow network, or closed chat should
            # never interrupt the reply itself. Do not log message contents.
            logger.debug("Noya chat-action unavailable: %s", type(exc).__name__)

    async def _loop(self):
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._changed.clear()
            await self._send()

    async def start(self):
        await self._send()
        self._task = asyncio.create_task(self._loop(), name="noya-chat-action")

    async def set_action(self, action: str):
        if action not in _VALID_ACTIONS:
            raise ValueError("Unsupported Telegram chat action")
        if self.action != action:
            self.action = action
            self._changed.set()

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


@asynccontextmanager
async def show_chat_action(message, action: str = "typing"):
    """Send and refresh a status until the caller has sent the response.

    Nested voice/photo generation in the same chat reuses the parent's task,
    rather than causing concurrent conflicting typing/recording statuses.
    """
    current = _ACTIVE_ACTION.get()
    bot = getattr(message, "bot", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    thread_id = getattr(message, "message_thread_id", None)
    if (current is not None and current.bot is bot
            and current.chat_id == chat_id and current.message_thread_id == thread_id):
        previous = current.action
        await current.set_action(action)
        try:
            yield current
        finally:
            await current.set_action(previous)
        return

    sender = NoyaChatAction(message, action=action)
    token = _ACTIVE_ACTION.set(sender)
    try:
        await sender.start()
        yield sender
    finally:
        try:
            await sender.stop()
        finally:
            _ACTIVE_ACTION.reset(token)


async def set_active_chat_action(action: str) -> bool:
    """Switch a running request's status when the agent selects a media tool."""
    current = _ACTIVE_ACTION.get()
    if current is None:
        return False
    await current.set_action(action)
    return True


def with_chat_action(action: str = "typing"):
    """Decorate only Noya answer handlers, never unrelated group messages."""
    def decorate(handler):
        @wraps(handler)
        async def wrapped(message, *args, **kwargs):
            async with show_chat_action(message, action=action):
                return await handler(message, *args, **kwargs)
        return wrapped
    return decorate
