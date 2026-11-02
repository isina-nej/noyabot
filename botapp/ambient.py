"""Optional low-frequency, opt-in ambient conversation in group chats.

An ambient reply is not a monitoring daemon: it is triggered only by updates
Telegram already sends to the bot, in groups where collaboration is enabled.
It never automatically downloads/transcribes every bystander's attachment.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from time import monotonic

_lock = threading.Lock()
_groups: OrderedDict[int, tuple[int, float]] = OrderedDict()
_MAX_GROUPS = 500


def _number(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(os.getenv(key, str(default)))
    except ValueError:
        number = default
    return min(max(number, minimum), maximum)


def should_join_group_conversation(message, *, now: float | None = None) -> bool:
    """Choose a bounded chance to participate after a real sequence of messages.

    Disabled unless explicitly enabled. Decisions depend on observed message
    count and cooldown (not random sampling), and never trigger on bot messages,
    commands, or messages replying to someone else.
    """
    if os.getenv("NOYA_AMBIENT_REPLIES_ENABLED", "false").casefold() not in {"1", "true", "yes", "on"}:
        return False
    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)
    if str(getattr(chat, "type", "")) not in {"group", "supergroup"}:
        return False
    if sender is None or getattr(sender, "is_bot", False):
        return False
    if getattr(message, "reply_to_message", None) is not None:
        return False
    text = str(getattr(message, "text", "") or "").strip()
    if not 8 <= len(text) <= 400 or text.startswith(("/", "!", "#")):
        return False
    if "@" in text:
        return False
    # Never spontaneously insert the bot into an existing admin operation.
    if any(text.casefold().startswith(x) for x in ("warn", "mute", "ban", "lock", "delete", "اخطار", "بن ", "سکوت", "قفل", "حذف")):
        return False
    min_messages = _number("NOYA_AMBIENT_MIN_MESSAGES", 12, 6, 200)
    cooldown = _number("NOYA_AMBIENT_COOLDOWN_SECONDS", 1800, 600, 86400)
    stamp = monotonic() if now is None else now
    chat_id = int(chat.id)
    with _lock:
        count, last = _groups.pop(chat_id, (0, -1e20))
        if stamp - last >= cooldown:
            count += 1
        can_reply = count >= min_messages and stamp - last >= cooldown
        _groups[chat_id] = (0 if can_reply else count, stamp if can_reply else last)
        while len(_groups) > _MAX_GROUPS:
            _groups.popitem(last=False)
        return can_reply


def reset_ambient_state_for_tests():
    with _lock:
        _groups.clear()
