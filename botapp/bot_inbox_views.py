"""Staff-only Django inbox API backed by NoyaBot's per-bot message archive.

Never serves a bot token to the browser. Uses the bot's existing backend token
only for operator-initiated sends. No independent Telegram update consumer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db.models import Count, Max
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from botapp.bot_inbox import archive_manual_send
from botapp.models import BotInboxMessage
from botapp.telegram_config import BOT_TOKENS

logger = logging.getLogger(__name__)


def _staff_only(view):
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "login required"}, status=401)
        if not request.user.is_active or not request.user.is_staff:
            return JsonResponse({"error": "staff access required"}, status=403)
        if not getattr(settings, "BOT_INBOX_ENABLED", False):
            return JsonResponse({"error": "bot inbox is disabled"}, status=404)
        return view(request, *args, **kwargs)
    return wrapped


def _int_field(raw, name: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(name + " must be an integer")
    number = int(raw)
    if not number or abs(number) > 10**16:
        raise ValueError(name + " is out of range")
    return number


def _message_dict(row: BotInboxMessage) -> dict:
    return {
        "id": row.message_id,
        "chat_id": str(row.chat_id),
        "bot_id": str(row.bot_id),
        "sender_id": str(row.sender_user_id) if row.sender_user_id else None,
        "sender_name": row.sender_name,
        "sender_username": row.sender_username,
        "outgoing": row.is_outgoing,
        "text": row.text,
        "caption": row.caption,
        "type": row.content_type,
        "has_media": bool(row.media_file_id),
        "reply_to_id": row.reply_to_message_id,
        "date": (row.message_date or row.archived_at).isoformat(),
        "edited": bool(row.edited_at),
    }


@never_cache
@require_GET
@_staff_only
def inbox_page(request):
    return render(request, "botapp/bot_inbox.html", {
        "bot_web_url": getattr(settings, "BOT_WEB_URL", ""),
        "allow_send": bool(getattr(settings, "BOT_INBOX_SEND_ENABLED", False)),
    })


@never_cache
@require_GET
@_staff_only
def inbox_bots(request):
    result = list(
        BotInboxMessage.objects.values("bot_id")
        .annotate(messages=Count("id"), chats=Count("chat_id", distinct=True))
        .order_by("bot_id")[:20]
    )
    return JsonResponse({
        "bots": [
            {"id": str(item["bot_id"]), "messages": item["messages"],
             "chats": item["chats"]}
            for item in result
        ],
        "send_enabled": bool(getattr(settings, "BOT_INBOX_SEND_ENABLED", False)),
    })


@never_cache
@require_GET
@_staff_only
def inbox_chats(request):
    try:
        bot_id = _int_field(request.GET.get("bot_id"), "bot_id")
        limit = min(100, max(1, int(request.GET.get("limit", 80))))
        offset = int(request.GET.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError("invalid offset")
        query = str(request.GET.get("q", ""))[:80].strip()
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid request"}, status=400)

    # Group by chat, not by message; a high-volume supergroup must not hide
    # low-volume private messages just because it generated many updates.
    candidates = BotInboxMessage.objects.filter(bot_id=bot_id)
    if query:
        from django.db.models import Q
        candidates = candidates.filter(
            Q(chat_title__icontains=query) |
            Q(sender_name__icontains=query) |
            Q(sender_username__icontains=query)
        )
    groups = list(
        candidates.values("chat_id")
        .annotate(last=Max(Coalesce("message_date", "archived_at")))
        .order_by("-last")[offset:offset + limit + 1]
    )
    has_more = len(groups) > limit
    groups = groups[:limit]
    chats = []
    for group in groups:
        row = (BotInboxMessage.objects.filter(
            bot_id=bot_id, chat_id=group["chat_id"],
        ).order_by("-message_date", "-message_id", "-id").first())
        if row is None:
            continue
        title = row.chat_title
        if row.chat_type == "private":
            first = BotInboxMessage.objects.filter(
                bot_id=bot_id, chat_id=row.chat_id, is_outgoing=False,
            ).order_by("-message_date", "-message_id").first()
            title = (first.chat_title or first.sender_name) if first else title
        chats.append({
            "id": str(row.chat_id),
            "title": title or str(row.chat_id),
            "type": row.chat_type,
            "preview": (row.text or row.caption or "[" + row.content_type + "]")[:140],
            "last": (row.message_date or row.archived_at).isoformat(),
            "last_sender": row.sender_name,
        })
    return JsonResponse({
        "chats": chats,
        "next_offset": offset + limit if has_more else None,
    })


@never_cache
@require_GET
@_staff_only
def inbox_messages(request):
    try:
        bot_id = _int_field(request.GET.get("bot_id"), "bot_id")
        chat_id = _int_field(request.GET.get("chat_id"), "chat_id")
        before = request.GET.get("before")
        after = request.GET.get("after")
        if before and after:
            raise ValueError("before and after cannot be combined")
        qs = BotInboxMessage.objects.filter(bot_id=bot_id, chat_id=chat_id)
        if before:
            qs = qs.filter(message_id__lt=_int_field(before, "before"))
        if after:
            qs = qs.filter(message_id__gt=_int_field(after, "after"))
        limit = min(100, max(1, int(request.GET.get("limit", 60))))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid request"}, status=400)

    rows = list(qs.order_by("-message_id")[:limit])
    return JsonResponse({
        "messages": [_message_dict(row) for row in reversed(rows)],
        "has_more": len(rows) == limit and not after,
    })


def _token_for_bot(bot_id: int) -> str | None:
    ids = [part.strip() for part in os.getenv("BOT_INBOX_BOT_IDS", "").split(",") if part.strip()]
    if not ids or len(ids) != len(BOT_TOKENS):
        return None
    if len(set(ids)) != len(ids):
        return None
    try:
        match = ids.index(str(bot_id))
    except ValueError:
        return None
    return BOT_TOKENS[match]


async def _send_checked(bot_id: int, token: str, chat_id: int, text: str,
                        reply_to_id: int | None):
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        if int(me.id) != bot_id:
            raise ValueError("Configured bot id does not match the token")
        sent = await bot.send_message(
            chat_id=chat_id, text=text,
            reply_to_message_id=reply_to_id,
            request_timeout=25,
        )
        return sent
    finally:
        await bot.session.close()


@never_cache
@require_POST
@_staff_only
def inbox_send(request):
    if not getattr(settings, "BOT_INBOX_SEND_ENABLED", False):
        return JsonResponse({"error": "sending is disabled"}, status=403)
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        bot_id = _int_field(body.get("bot_id"), "bot_id")
        chat_id = _int_field(body.get("chat_id"), "chat_id")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ValueError("text must be 1-4096 characters")
        text = text.strip()
        reply = body.get("reply_to_message_id")
        reply_id = _int_field(reply, "reply_to_message_id") if reply is not None else None
    except (ValueError, TypeError, UnicodeError):
        return JsonResponse({"error": "invalid message"}, status=400)

    # Refuse sends to any arbitrary chat. The selected bot must have actually
    # observed the chat before the staff member can compose a response.
    if not BotInboxMessage.objects.filter(bot_id=bot_id, chat_id=chat_id).exists():
        return JsonResponse({"error": "unknown bot chat"}, status=404)
    token = _token_for_bot(bot_id)
    if token is None:
        return JsonResponse({"error": "verified bot mapping is not configured"}, status=503)
    try:
        sent = async_to_sync(_send_checked)(bot_id, token, chat_id, text, reply_id)
        row = archive_manual_send(
            bot_id=bot_id, chat_id=chat_id, message=sent, text=text,
        )
    except Exception:
        # Timeout after Telegram received the send has an ambiguous outcome.
        # Do not retry automatically: the operator can inspect the chat first.
        logger.warning("Bot inbox send failed or outcome unknown: bot=%s chat=%s", bot_id, chat_id)
        return JsonResponse({
            "error": "Send failed or outcome unknown; check Telegram before retrying",
        }, status=502)
    return JsonResponse({"message": _message_dict(row)}, status=201)
