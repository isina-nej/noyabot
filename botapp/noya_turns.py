"""Best-effort per-speaker ordering for slow group-chat timeout notices.

Telegram can dispatch multiple messages concurrently. A five-minute timeout
for an already superseded social turn must not interrupt newer discussion.
This is deliberately scoped to timeout messages, not successful answers or
tool side effects. Multi-replica deployments need a shared state adapter.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from time import monotonic

_lock = threading.Lock()
_latest: OrderedDict[tuple[int, int, int, int], tuple[int, float]] = OrderedDict()
MAX_KEYS = 2048
TTL_SECONDS = 1800


def _key(message, bot_id: int) -> tuple[int, int, int, int] | None:
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    user_id = getattr(user, "id", None)
    chat_id = getattr(chat, "id", None)
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return None
    return (int(bot_id), int(chat_id), int(user_id),
            int(getattr(message, "message_thread_id", None) or 0))


def remember_turn(message, bot_id: int) -> None:
    key = _key(message, bot_id)
    msg_id = getattr(message, "message_id", None)
    if key is None or not isinstance(msg_id, int):
        return
    now = monotonic()
    with _lock:
        previous_id = _latest.get(key, (0, 0))[0]
        _latest[key] = (max(previous_id, msg_id), now)
        _latest.move_to_end(key)
        while _latest and (
            len(_latest) > MAX_KEYS
            or next(iter(_latest.values()))[1] < now - TTL_SECONDS
        ):
            _latest.popitem(last=False)


def superseded_timeout(message, bot_id: int) -> bool:
    if os.getenv("NOYA_SUPPRESS_SUPERSEDED_TIMEOUTS", "true").strip().lower() not in (
        "true", "1", "yes", "on",
    ):
        return False
    if str(getattr(getattr(message, "chat", None), "type", "")) not in ("group", "supergroup"):
        return False
    key = _key(message, bot_id)
    msg_id = getattr(message, "message_id", None)
    if key is None or not isinstance(msg_id, int):
        return False
    with _lock:
        item = _latest.get(key)
        return bool(item and item[0] > msg_id and item[1] > monotonic() - TTL_SECONDS)


def reset_for_tests():
    with _lock:
        _latest.clear()
