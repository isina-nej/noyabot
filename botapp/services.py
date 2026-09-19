import logging
import os
import re
from typing import Any
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
    use_agent: bool = True,
) -> tuple[str, dict]:
    """Hermes-style: call agent loop with tools, fall back to direct API."""
    t0 = monotonic()
    logger.info(
        "[NOYA-TIMING] ▶ START call_noya_api session=%s speaker_id=%s question=%r agent=%s",
        session_id, speaker_user_id, (question or "")[:60], use_agent,
    )
    # ── Precedence: Search / URL intent overrides clock short-circuit ──
    from botapp.web import resolve_search_intent, SearchDecision
    decision, _ = resolve_search_intent(question or "")

    if decision != SearchDecision.MUST_SEARCH and is_clock_question(question):
        dt = (monotonic() - t0) * 1000
        logger.info("[NOYA-TIMING] ⏱ Clock direct response in %.1fms", dt)
        return format_clock_reply(), {}

    # ── Agent loop (Hermes-style tool calling) ──
    if use_agent:
        from botapp.agent.loop import run_agent_loop
        from botapp.ai.prompts import get_noya_system_prompt
        t_agent = monotonic()
        system_prompt = get_noya_system_prompt()
        reply, metadata = await run_agent_loop(
            question=question,
            session_id=session_id,
            system_prompt=system_prompt,
        )
        agent_ms = (monotonic() - t_agent) * 1000
        total_ms = (monotonic() - t0) * 1000
        logger.info(
            "[NOYA-TIMING] 🤖 Agent loop done in %.1fms (total=%.1fms, tools=%s)",
            agent_ms, total_ms, metadata.get("tools_used", []),
        )
        return reply, metadata

    # ── Fallback: direct API call (old path) ──
    return await _call_noya_api_direct(question, session_id, speaker_user_id=speaker_user_id, speaker_name=speaker_name, images=images)


async def _call_noya_api_direct(
    question: str,
    session_id: str,
    *,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
    images: list[dict] | None = None,
) -> tuple[str, dict]:
    """Legacy direct API path — no tool calling, single LLM call."""
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    if not api_key:
        return "خطا در ارتباط با نویا. لطفاً دوباره تلاش کنید.", {}
    url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    model = os.getenv("NOYA_MODEL", "FastText").strip()
    if images:
        model = os.getenv("NOYA_VISION_MODEL", model).strip() or model
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t_search = monotonic()
    search_block = await maybe_web_search(question)
    search_ms = (monotonic() - t_search) * 1000
    logger.info("[NOYA-TIMING] 🔍 Search %.1fms", search_ms)
    urls = extract_urls(question)
    if urls:
        page_content = await fetch_url_content(urls[0])
        url_block = f"[WEB_PAGE_CONTENT url={urls[0]}]\n{page_content}\n[/WEB_PAGE_CONTENT]"
        search_block = f"{search_block}\n\n{url_block}" if search_block else url_block
    payload = {
        "model": model, "stream": False,
        "messages": build_ai_messages(question, speaker_user_id=speaker_user_id, speaker_name=speaker_name, images=images, search_block=search_block),
    }
    t_ai = monotonic()
    try:
        async with httpx.AsyncClient(timeout=NOYA_API_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            ai_ms = (monotonic() - t_ai) * 1000
            logger.info("[NOYA-TIMING] 🤖 Direct API %.1fms chars=%d", ai_ms, len(content))
            return content, {}
    except httpx.TimeoutException:
        return "نویا این لحظه شلوغه و جواب نداد.", {}
    except Exception:
        logger.exception("[NOYA-TIMING] ❌ Direct API failed")
        return "خطا در ارتباط با نویا.", {}


async def _translate_prompt_for_image(prompt: str) -> str:
    """Translate a Persian image prompt to English via 9Router for better quality."""
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    base_url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    if not api_key:
        return prompt
    # ponytail: direct LLM call; extract to shared helper when 2nd caller appears
    try:
        t0 = monotonic()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                base_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": os.getenv("NOYA_MODEL", "FastText"),
                    "messages": [
                        {"role": "system", "content": "You are a prompt translator. Convert the user's Persian image description into a detailed, vivid English prompt suitable for AI image generation (Stable Diffusion / Flux). Output ONLY the English prompt, nothing else. Add artistic quality keywords like 'highly detailed, professional, 4k, cinematic lighting' when appropriate."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:
                from botapp.agent.loop import _parse_sse_stream
                data = _parse_sse_stream(resp.text) or {}
            en = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            logger.info("[NOYA-TIMING] 🔤 Prompt translation %.1fms: %r → %r", (monotonic() - t0) * 1000, prompt, en[:100])
            return en if en else prompt
    except Exception:
        logger.warning("Prompt translation failed, using original Persian", exc_info=True)
        return prompt


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

    # Translate Persian prompt to English for much better image quality
    en_prompt = await _translate_prompt_for_image(prompt)

    model = os.getenv("NOYA_IMAGE_MODEL", "ag/gemini-3.1-flash-image").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": en_prompt,
        "n": 1,
        "size": "auto",
        "quality": "auto",
        "background": "auto",
        "image_detail": "high",
        "output_format": "png",
    }
    try:
        t0 = monotonic()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(img_url, headers=headers, json=payload)
            resp.raise_for_status()
            logger.info("[NOYA-TIMING] 🖼️ Image generation took %.1fms", (monotonic() - t0) * 1000)
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
        logger.exception("Noya image generation failed prompt=%s", en_prompt[:80])
    return None


def _extract_image_bytes(content: Any) -> bytes | None:
    """Extract decoded image binary bytes from multimodal response or text with base64."""
    import base64 as b64mod
    import re

    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                url = part.get("image_url", {}).get("url", "") or part.get("url", "")
                if "base64," in url:
                    b64_str = url.split("base64,", 1)[1].split(")", 1)[0].strip()
                    try:
                        dec = b64mod.b64decode(b64_str)
                        if dec.startswith((b"\xff\xd8", b"\x89PNG", b"GIF", b"RIFF")):
                            return dec
                    except Exception:
                        pass
                data = part.get("inline_data", {}).get("data") or part.get("source", {}).get("data")
                if data:
                    try:
                        dec = b64mod.b64decode(data)
                        if dec.startswith((b"\xff\xd8", b"\x89PNG", b"GIF", b"RIFF")):
                            return dec
                    except Exception:
                        pass

    if isinstance(content, str):
        # 1. Regex for data:image/...;base64,<data>
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
        if m:
            try:
                dec = b64mod.b64decode(m.group(1))
                if dec.startswith((b"\xff\xd8", b"\x89PNG", b"GIF", b"RIFF")):
                    return dec
            except Exception:
                pass
        # 2. Extract longest base64 sequence from string
        matches = re.findall(r"[A-Za-z0-9+/=]{100,}", content)
        for b64_cand in matches:
            try:
                dec = b64mod.b64decode(b64_cand)
                if dec.startswith((b"\xff\xd8", b"\x89PNG", b"GIF", b"RIFF")):
                    return dec
            except Exception:
                pass
    return None


async def edit_noya_image(instruction: str, image_data: bytes, image_mime: str = "image/jpeg") -> bytes | None:
    """Edit an image via Gemini vision model: send image + edit instruction, get new image back."""
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    base_url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    if not api_key:
        logger.error("NOYA_API_KEY not configured for image edit")
        return None

    en_instruction = await _translate_prompt_for_image(instruction)

    import base64 as b64mod
    data_url = f"data:{image_mime};base64,{b64mod.b64encode(image_data).decode('ascii')}"

    payload = {
        "model": os.getenv("NOYA_IMAGE_EDIT_MODEL", "ag/gemini-3.1-flash-image"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": f"Edit this image: {en_instruction}. Return ONLY the edited image, no text."},
                ],
            }
        ],
        "max_tokens": 4096,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        t0 = monotonic()
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(base_url, headers=headers, json=payload)
            resp.raise_for_status()
            logger.info("[NOYA-TIMING] ✏️ Image edit took %.1fms", (monotonic() - t0) * 1000)
            try:
                data = resp.json()
            except Exception:
                from botapp.agent.loop import _parse_sse_stream
                data = _parse_sse_stream(resp.text) or {}

            choices = data.get("choices") or []
            if not choices:
                logger.warning("Image edit returned no choices: %s", resp.text[:200])
                return None
            msg = choices[0].get("message", {})
            content = msg.get("content", "")

            img_bytes = _extract_image_bytes(content)
            if img_bytes:
                return img_bytes

            logger.warning("Image edit: model returned unparseable content: %s", str(content)[:200])
            return None
    except Exception:
        logger.exception("Noya image edit failed instruction=%s", en_instruction[:80])
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


# ---------- Noya TTS (Text-to-Speech via 9Router) ----------

_NOYA_TTS_PROMPT = """\
Generate speech audio from the transcript below.
Do not read the instructions or section headings aloud.

# AUDIO PROFILE
Name: Noya
Role: Casual Persian conversational personality

Personality:
Playful, cheerful, warm, expressive, confident and mischievous.

# THE SCENE
Noya is casually chatting with a close friend through voice messages.
The atmosphere is relaxed, playful and informal.

# DIRECTOR'S NOTES

Style:
Use a playful, warm and expressive conversational delivery.
Maintain a subtle vocal smile.
Sound naturally amused when teasing.
Keep the performance spontaneous rather than theatrical.

Pacing:
Use a medium-fast conversational pace.
Use short natural pauses.
Allow speed to change naturally according to emotion.
Slow down slightly for emphasis.

Accent:
Use natural contemporary conversational Persian from Tehran.
Avoid formal broadcast-style pronunciation.

Articulation:
Keep speech clear but conversational.
Avoid excessive enunciation.
Use natural connected speech.

Breathing:
Keep breathing subtle and natural.

Dynamics:
Use natural changes in energy and emphasis.
Increase energy slightly for excited reactions.
Avoid unnecessary shouting.

# SAMPLE CONTEXT
Noya is responding to a close friend's message.
She feels comfortable and is lightly teasing them.

# TRANSCRIPT
"""


async def generate_noya_tts(text: str, *, as_noya: bool = True) -> bytes | None:
    """Convert text to speech via 9Router /v1/audio/speech.

    When ``as_noya`` is True the full Noya audio-profile prompt wraps the
    transcript for expressive, in-character delivery.  Otherwise the raw text
    is sent for a neutral read.

    Returns MP3 bytes on success, None on failure.
    """
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    if not api_key:
        logger.error("NOYA_API_KEY not configured for TTS")
        return None

    base_url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    if "/chat/completions" in base_url:
        tts_url = base_url.replace("/chat/completions", "/audio/speech")
    else:
        tts_url = base_url.rstrip("/") + "/audio/speech"

    model = os.getenv("NOYA_TTS_MODEL", "gemini/gemini-3.1-flash-tts-preview/Zephyr").strip()

    if as_noya:
        # Wrap with the full Noya audio-profile prompt
        input_text = _NOYA_TTS_PROMPT + text.strip()
    else:
        input_text = text.strip()

    # ponytail: 4000 bytes Cloud TTS limit per field; truncate if needed
    max_bytes = 7500
    if len(input_text.encode("utf-8")) > max_bytes:
        while len(input_text.encode("utf-8")) > max_bytes and len(input_text) > 100:
            input_text = input_text[: len(input_text) - 50]
        input_text = input_text.rstrip() + "…"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": input_text,
    }

    try:
        t0 = monotonic()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(tts_url, headers=headers, json=payload)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            elapsed = (monotonic() - t0) * 1000
            if "audio" in content_type or "octet-stream" in content_type:
                logger.info(
                    "[NOYA-TIMING] 🔊 TTS took %.1fms (bytes=%d, model=%s)",
                    elapsed, len(resp.content), model,
                )
                return resp.content
            # Might be JSON with base64
            try:
                data = resp.json()
                import base64
                audio_b64 = data.get("audio") or data.get("data", [{}])[0].get("b64_json", "")
                if audio_b64:
                    logger.info("[NOYA-TIMING] 🔊 TTS (b64) took %.1fms", elapsed)
                    return base64.b64decode(audio_b64)
            except Exception:
                pass
            logger.warning("TTS unexpected content-type=%s body=%s", content_type, resp.text[:200])
            return None
    except httpx.TimeoutException:
        logger.warning("[NOYA-TIMING] ⚠️ TTS timed out")
        return None
    except Exception:
        logger.exception("TTS generation failed")
        return None


# ---------- Web Page Content Fetcher (like Hermes web_extract) ----------

_STRIP_TAGS_RE = re.compile(r"<(script|style|nav|footer|header|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


async def fetch_url_content(url: str, *, max_chars: int = 8000) -> str:
    """Fetch a web page and return clean readable text content with SSRF safety and semantic parsing."""
    from botapp.web import default_web_service
    try:
        t0 = monotonic()
        doc = await default_web_service.fetch_page(url)
        elapsed = (monotonic() - t0) * 1000
        text = doc.text
        logger.info("[NOYA-TIMING] 🌐 Fetched %s in %.1fms (chars=%d)", url[:60], elapsed, len(text))
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[ادامه محتوا کوتاه شد]"
        return text if text else "[صفحه خالی بود]"
    except Exception as exc:
        logger.warning("Fetch URL failed: %s error=%s", url[:60], exc)
        return f"[خطا در باز کردن لینک: {exc}]"
