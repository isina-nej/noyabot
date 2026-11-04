"""Shared Telegram token parsing without importing the Bot dispatcher."""
from __future__ import annotations

import os
import re


def parse_bot_tokens(bot_tokens: str = "", bot_token: str = "") -> tuple[str, ...]:
    values = re.split(r"[\s,]+", bot_tokens.strip()) if bot_tokens.strip() else []
    if bot_token.strip():
        values.append(bot_token.strip())
    return tuple(dict.fromkeys(value for value in values if value))


BOT_TOKENS = parse_bot_tokens(os.getenv("BOT_TOKENS", ""), os.getenv("BOT_TOKEN", ""))
