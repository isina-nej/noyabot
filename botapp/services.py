import logging
import os
import re
from time import monotonic
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from botapp.models import GroupQuota, GroupSettings, ModerationLog, Warning

logger = logging.getLogger(__name__)
_flood_events = defaultdict(deque)
_duplicate_events = defaultdict(deque)

# ponytail: 45s beats Telegram's ~60s "bot is not responding" feel and stops the
# router's long provider-fallback chain from burning 2 minutes of user patience.
# Raise via env if the upstream combo genuinely needs longer.
NOYA_API_TIMEOUT = float(os.getenv("NOYA_API_TIMEOUT", "45"))


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff\u2060\u00ad]")
# Scheme / www links, bare t.me / telegram.me, and common bare domains.
_URL_RE = re.compile(
    r"(?i)"
    r"(?:https?://|www\.)[^\s<>()]+"
    r"|(?:t\.me|telegram\.me)/[^\s<>()]+"
    r"|(?<![\w./@-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|org|net|ir|io|me|info|xyz|app|dev|co|ai|tv|cc|pro|site|online|shop|blog)(?:/[^\s<>()]*)?"
)


def normalize_text(text: str) -> str:
    cleaned = _ZERO_WIDTH_RE.sub("", text or "")
    return " ".join(cleaned.casefold().split())


def contains_blocked_word(text: str, blocked_words: list[str]) -> bool:
    normalized = normalize_text(text)
    for word in blocked_words:
        needle = normalize_text(word)
        if needle and needle in normalized:
            return True
    return False


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    # Strip zero-width chars that can split an otherwise obvious URL.
    cleaned = _ZERO_WIDTH_RE.sub("", text)
    found = _URL_RE.findall(cleaned)
    # Drop trailing punctuation commonly glued onto links in chat.
    return [url.rstrip(".,!?;:،؛»\"')]") for url in found]


def is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    normalized_url = url if "://" in url else f"https://{url}"
    host = (urlsplit(normalized_url).hostname or "").lower().removeprefix("www.")
    for domain in allowed_domains:
        allowed = domain.strip().lower().removeprefix("www.")
        if allowed and (host == allowed or host.endswith(f".{allowed}")):
            return True
    return False


def is_flooding(chat_id: int, user_id: int, limit: int, window_seconds: int, now=None) -> bool:
    now = now or timezone.now()
    events = _flood_events[(chat_id, user_id)]
    cutoff = now.timestamp() - max(window_seconds, 1)
    while events and events[0] <= cutoff:
        events.popleft()
    events.append(now.timestamp())
    return len(events) > max(limit, 1)


def is_duplicate_message(chat_id: int, user_id: int, text: str, limit: int, window_seconds: int, now=None) -> bool:
    now = now or timezone.now()
    events = _duplicate_events[(chat_id, user_id)]
    cutoff = now.timestamp() - max(window_seconds, 1)
    while events and events[0][0] <= cutoff:
        events.popleft()
    normalized = normalize_text(text)
    events.append((now.timestamp(), normalized))
    return sum(value == normalized for _, value in events) >= max(limit, 2)


@sync_to_async(thread_sensitive=True)
def get_or_create_moderation_settings(chat_id: int, chat_title: str = "") -> GroupSettings:
    group, _ = GroupSettings.objects.get_or_create(
        chat_id=chat_id,
        defaults={"chat_title": chat_title},
    )
    if chat_title and group.chat_title != chat_title:
        group.chat_title = chat_title
        group.save(update_fields=["chat_title", "updated_at"])
    return group


@sync_to_async(thread_sensitive=True)
def create_moderation_log(
    group_id,
    action,
    target_user_id=None,
    target_name="",
    actor_user_id=None,
    actor_name="",
    reason="",
    duration_minutes=None,
):
    return ModerationLog.objects.create(
        group_id=group_id,
        target_user_id=target_user_id,
        target_name=target_name,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        action=action,
        reason=reason,
        duration_minutes=duration_minutes,
    )


@sync_to_async(thread_sensitive=True)
def add_warning(
    group_id,
    user_id,
    user_name,
    issued_by_user_id,
    issued_by_name,
    reason,
    expiry_days,
):
    now = timezone.now()
    Warning.objects.filter(
        group_id=group_id,
        user_id=user_id,
        revoked_at__isnull=True,
        expires_at__lt=now,
    ).update(revoked_at=now)
    Warning.objects.create(
        group_id=group_id,
        user_id=user_id,
        user_name=user_name,
        issued_by_user_id=issued_by_user_id,
        issued_by_name=issued_by_name,
        reason=reason,
        expires_at=now + timedelta(days=max(expiry_days, 1)),
    )
    return Warning.objects.filter(
        group_id=group_id,
        user_id=user_id,
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).count()


@sync_to_async(thread_sensitive=True)
def clear_warnings(group_id, user_id):
    return Warning.objects.filter(
        group_id=group_id,
        user_id=user_id,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())


@sync_to_async(thread_sensitive=True)
def get_active_warning_count(group_id, user_id):
    now = timezone.now()
    return Warning.objects.filter(
        group_id=group_id,
        user_id=user_id,
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).count()


@sync_to_async(thread_sensitive=True)
def purge_old_moderation_logs(group_id, retention_days):
    cutoff = timezone.now() - timedelta(days=max(retention_days, 1))
    return ModerationLog.objects.filter(group_id=group_id, created_at__lt=cutoff).delete()[0]


@sync_to_async(thread_sensitive=True)
def consume_group_quota(chat_id: int, chat_title: str = "") -> bool:
    """Atomically consume one daily request from a group's quota.

    ``daily_prompt_limit=0`` (default) means unlimited — always allowed.
    """
    today = timezone.localdate()

    with transaction.atomic():
        quota, _ = GroupQuota.objects.select_for_update().get_or_create(
            chat_id=chat_id,
            defaults={"chat_title": chat_title, "daily_prompt_limit": 0},
        )

        changed_fields = []
        if chat_title and quota.chat_title != chat_title:
            quota.chat_title = chat_title
            changed_fields.append("chat_title")
        if quota.last_reset < today:
            quota.tokens_used_today = 0
            quota.last_reset = today
            changed_fields.extend(("tokens_used_today", "last_reset"))
        if changed_fields:
            quota.save(update_fields=list(dict.fromkeys(changed_fields)))

        # 0 = unlimited for every group.
        if quota.daily_prompt_limit == 0:
            return True

        if quota.tokens_used_today >= quota.daily_prompt_limit:
            return False

        GroupQuota.objects.filter(pk=quota.pk).update(
            tokens_used_today=F("tokens_used_today") + 1,
        )
        return True


from botapp.ai import build_ai_messages
from botapp.noya_clock import format_clock_reply, is_clock_question
from botapp.noya_search import maybe_web_search

async def call_noya_api(
    question: str,
    session_id: str,
    *,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
    images: list[dict] | None = None,
) -> str:
    t0 = monotonic()
    logger.info(
        "[NOYA-TIMING] ▶ START call_noya_api session=%s speaker_id=%s question=%r",
        session_id,
        speaker_user_id,
        (question or "")[:60],
    )
    if is_clock_question(question):
        dt = (monotonic() - t0) * 1000
        logger.info("[NOYA-TIMING] ⏱ Clock direct response in %.1fms", dt)
        return format_clock_reply()

    api_key = os.getenv("NOYA_API_KEY", "").strip()
    if not api_key:
        logger.error("[NOYA-TIMING] ❌ NOYA_API_KEY is not configured")
        return "خطا در ارتباط با نویا. لطفاً دوباره تلاش کنید."

    url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    # Prefer a vision-capable override when images are attached; NoyaBest already
    # supports multimodal on the current 9router stack, so default stays NOYA_MODEL.
    model = os.getenv("NOYA_MODEL", "TinkeraBot").strip()
    if images:
        model = os.getenv("NOYA_VISION_MODEL", model).strip() or model
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t_search = monotonic()
    search_block = await maybe_web_search(question)
    search_ms = (monotonic() - t_search) * 1000
    if search_block:
        logger.info("[NOYA-TIMING] 🔍 Web search took %.1fms (content_len=%d)", search_ms, len(search_block))
    else:
        logger.info("[NOYA-TIMING] 🔍 Web search skipped/empty in %.1fms", search_ms)

    payload = {
        "model": model,
        "stream": False,
        "messages": build_ai_messages(
            question,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            images=images,
            search_block=search_block,
        ),
    }
    t_ai = monotonic()
    logger.info("[NOYA-TIMING] 🤖 Calling 9Router model=%s url=%s ...", model, url)
    try:
        async with httpx.AsyncClient(timeout=NOYA_API_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            ai_ms = (monotonic() - t_ai) * 1000
            content = data["choices"][0]["message"]["content"]
            total_ms = (monotonic() - t0) * 1000
            logger.info(
                "[NOYA-TIMING] 🤖 9Router replied in %.1fms (status=%s, chars=%d) | TOTAL API DURATION: %.1fms",
                ai_ms,
                response.status_code,
                len(content),
                total_ms,
            )
            return content
    except httpx.TimeoutException:
        ai_ms = (monotonic() - t_ai) * 1000
        logger.warning("[NOYA-TIMING] ⚠️ Noya AI API request timed out after %.1fms (cap=%ss)", ai_ms, NOYA_API_TIMEOUT)
        return "نویا این لحظه شلوغه و جواب نداد. یک دقیقه دیگه دوباره امتحان کن."
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        ai_ms = (monotonic() - t_ai) * 1000
        logger.exception("[NOYA-TIMING] ❌ Noya AI API request failed after %.1fms", ai_ms)
        return "خطا در ارتباط با نویا. لطفاً دوباره تلاش کنید."


async def generate_noya_image(prompt: str) -> bytes | None:
    """Generate image via 9Router /v1/images/generations endpoint."""
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    if not api_key:
        logger.error("NOYA_API_KEY is not configured for image generation")
        return None

    base_url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    if "/chat/completions" in base_url:
        img_url = base_url.replace("/chat/completions", "/images/generations")
    else:
        img_url = base_url.rstrip("/") + "/images/generations"

    model = os.getenv("NOYA_IMAGE_MODEL", "image").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "512x512",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(img_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            item = (data.get("data") or [{}])[0]
            b64 = item.get("b64_json")
            if b64:
                import base64
                return base64.b64decode(b64)
            img_url_resp = item.get("url")
            if img_url_resp:
                r = await client.get(img_url_resp)
                r.raise_for_status()
                return r.content
    except Exception:
        logger.exception("Noya image generation failed prompt=%s", prompt[:80])
    return None


async def call_ai_api(
    api_url: str,
    question: str,
    session_id: str,
    *,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
    images: list[dict] | None = None,
) -> str:
    if is_clock_question(question):
        return format_clock_reply()
    search_block = await maybe_web_search(question)
    payload = {
        "sessionId": session_id,
        "messages": build_ai_messages(
            question,
            speaker_user_id=speaker_user_id,
            speaker_name=speaker_name,
            images=images,
            search_block=search_block,
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(api_url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        return "زمان پاسخ تمام شد. لطفا دوباره تلاش کنید."
    except (httpx.HTTPError, ValueError):
        logger.exception("AI API request failed")
        return "خطا در ارتباط با هوش مصنوعی. لطفا دوباره تلاش کنید."

    content = data.get("content") if isinstance(data, dict) else None
    return content if isinstance(content, str) and content.strip() else "پاسخی دریافت نشد."
