import logging
import random
import re

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

from botapp.channel_insights import (
    ChannelInsightUnavailable,
    analysis_limit_from_text as analysis_limit_from_input,
    build_analysis_prompt,
    fetch_channel_digest,
    wants_channel_analysis,
)
from botapp.memory.integration import run_ai_with_memory
from botapp.noya_bot_chat import (
    allow_bot_to_bot_reply,
    is_other_bot_sender,
    is_self_bot_message,
)
from botapp.noya_context import build_noya_user_payload, strip_bot_address
from botapp.services import call_noya_api
from botapp.telegram_media import collect_noya_images
from botapp.telegram_rich import extract_message_body
from botapp.observability import logging as obs_logging

logger = logging.getLogger(__name__)

router = Router()

TELEGRAM_TEXT_LIMIT = 4096


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long text into ≤ *limit* chunks at line boundaries when possible."""
    body = (text or "").strip()
    if len(body) <= limit:
        return [body] if body else []
    chunks: list[str] = []
    while len(body) > limit:
        window = body[:limit]
        # Prefer breaking at the last paragraph, then last line, then whitespace.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(body[:cut].rstrip())
        body = body[cut:].lstrip()
    if body:
        chunks.append(body)
    return chunks

# Persian-aware word boundary around «نویا».
_NOYA_NAME_RE = re.compile(r"(?<![\wآ-ی])نویا(?![\wآ-ی])", re.IGNORECASE)
_NOYA_LATIN_NAME_RE = re.compile(
    r"(?<![\w])(?:noya|nuya|noia|nuia)(?![\w])", re.IGNORECASE
)
_AI_PREFIX_RE = re.compile(r"^(?:نویا|noya|nuya|noia|nuia)(?:\s|\u200c)", re.IGNORECASE)


async def _reply_html(message: types.Message, text: str) -> None:
    body = (text or "").strip() or "…"
    try:
        await message.reply(body, parse_mode="HTML")
    except TelegramBadRequest:
        await message.reply(body)


def _guest_question(message: types.Message, bot_username: str = "") -> str:
    text = extract_message_body(message)
    ask = strip_bot_address(text, bot_username) if bot_username else text.strip()
    if not ask and bot_username:
        ask = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()
    return build_noya_user_payload(
        message,
        ask or text.strip(),
        bot_username=bot_username,
        include_recent=False,
    )


def _channel_question(text: str, bot_username: str = "") -> tuple[str | None, bool]:
    """Extract the question from a channel post addressed to Noya.

    Returns ``(question, is_bare)`` where *is_bare* is True when the post
    contained only the bot name with no real question — the caller should
    reply with a canned response instead of invoking the AI.
    """
    text = text.strip()
    mention = bool(
        bot_username
        and re.search(rf"@{re.escape(bot_username)}\b", text, flags=re.IGNORECASE)
    )
    name_call = bool(
        _NOYA_NAME_RE.search(text) or _NOYA_LATIN_NAME_RE.search(text)
    )
    if not (mention or name_call):
        return None, False
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    text = _NOYA_NAME_RE.sub("", text)
    text = _NOYA_LATIN_NAME_RE.sub("", text)
    cleaned = text.strip()
    if not cleaned:
        return cleaned, True  # bare name, no question
    return cleaned, False


async def _send_channel_answer(location: types.Message, answer: str) -> None:
    """Edit/reply with the final answer; falls back to plain text on HTML errors."""
    body = (answer or "").strip()
    if not body:
        body = "…"
    try:
        await location.edit_text(body, parse_mode="HTML")
    except TelegramBadRequest:
        await location.edit_text("…")
        # Re-send in plain chunks so message-length / parse errors never eat
        # the whole answer silently.
        for chunk in split_telegram_text(body):
            try:
                await location.reply(chunk)
            except TelegramBadRequest as plain_error:
                logger.warning(
                    "channel_analysis_send_failed chat=%s error=%s",
                    getattr(getattr(location, "chat", None), "id", None),
                    type(plain_error).__name__,
                )
                await location.reply("پاسخ آماده شد ولی ارسالش ممکن نشد؛ دوباره امتحان کن.")


@router.channel_post(F.text | F.caption | F.rich_message)
async def channel_nouya_handler(message: types.Message):
    """Channel «نویا» post — exactly the group pipeline, plus MTProto analysis."""
    bot_user = await message.bot.get_me()
    body = extract_message_body(message)
    question, is_bare = _channel_question(body, bot_user.username or "")
    if question is None:
        return
    if is_bare:
        await message.reply(random.choice(RESPONSES))
        return
    images = await collect_noya_images(message.bot, message)

    # ── Channel-wide analysis via MTProto («نویا تحلیل …») ──
    if wants_channel_analysis(question):
        limit = analysis_limit_from_input(question)
        progress = await message.reply("دارم تاریخچهٔ کانال می‌خونم… چند دقیقه صبر کن ✍️")
        try:
            digest = await fetch_channel_digest(int(message.chat.id), limit)
            obs_logging.emit(
                "channel.analysis.digest_ready",
                message_count=digest.message_count,
                requested_limit=limit,
            )
            prompt = build_analysis_prompt(digest, question)
            answer, _meta = await call_noya_api(
                prompt,
                session_id=f"telegram:channel-analysis:{message.chat.id}:{message.message_id}",
                images=None,
            )
            await _send_channel_answer(progress, answer)
            return
        except ChannelInsightUnavailable as unavailable:
            obs_logging.emit("channel.analysis.unavailable", level="warning", reason=unavailable.code)
            await _send_channel_answer(progress, unavailable.user_message())
            return

    payload = build_noya_user_payload(message, question, bot_username=bot_user.username or "")
    progress = await message.reply("یک لحظه…")
    answer, _meta = await run_ai_with_memory(
        message,
        payload,
        call_noya_api,
        session_id=f"telegram:channel:{message.chat.id}:{message.message_id}",
        images=images or None,
    )
    await _send_channel_answer(progress, answer)


@router.guest_message()
async def guest_nouya_handler(message: types.Message):
    if not message.guest_query_id:
        return
    bot_user = await message.bot.get_me()
    question = _guest_question(message, bot_user.username or "")
    if not question:
        question = "به پیام اشاره‌شده پاسخ بده."
    images = await collect_noya_images(message.bot, message)
    progress = InlineQueryResultArticle(
        id="noya-guest-progress",
        title="Noya",
        input_message_content=InputTextMessageContent(message_text="یک لحظه…"),
    )
    sent = await message.answer_guest_query(result=progress)
    answer, _meta = await call_noya_api(
        question,
        session_id=f"telegram:guest:{message.chat.id}:{message.message_id}",
        images=images or None,
    )
    try:
        await message.bot.edit_message_text(
            inline_message_id=sent.inline_message_id,
            text=answer,
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        try:
            await message.bot.edit_message_text(
                inline_message_id=sent.inline_message_id,
                text=answer,
            )
        except TelegramBadRequest:
            pass


RESPONSES = [
    "جان؟ 😌",
    "هه، بگو ببینم",
    "جونم؟ ناز نکن ها",
    "آها؟ چی شده",
    "بله؟ من اینجام دیگه",
    "صدام کردی؟ بگو زود",
]


def _is_real_ai_invocation(message: types.Message, bot_user, text: str) -> bool:
    """True when this message belongs to the main AI path.

    Ported from the stashed easter-egg guard and adapted to the current
    structure: prefix triggers with a real question, @mention triggers, and
    replies to the bot must never be swallowed by the canned reply below.
    Bare «نویا» (no question) is NOT a real invocation — it keeps the
    canned easter-egg reply.
    """
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    lowered = t.lower()
    for prefix in ("نویا", "noya", "nuya", "noia", "nuia"):
        if lowered.startswith(prefix):
            rest = t[len(prefix):].strip(" \t,،:.-\u200c")
            if rest:
                return True
    username = (getattr(bot_user, "username", None) or "").lower()
    if username and f"@{username}" in lowered:
        return True
    replied = getattr(message, "reply_to_message", None)
    replied_from = getattr(replied, "from_user", None)
    if replied_from is not None and getattr(replied_from, "id", None) == getattr(bot_user, "id", None):
        return True
    return False


@router.message(F.text.regexp(_NOYA_NAME_RE))
async def nouya_mention_handler(message: types.Message):
    """Handle bare/mid-sentence «نویا» mentions that the main prefix path missed."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        raise SkipHandler()

    bot_user = await message.bot.get_me()
    if is_self_bot_message(message, self_bot_id=int(bot_user.id)):
        return

    # Real AI invocations belong to handle_text_message (main router runs
    # first); never steal them with the canned reply.
    if _is_real_ai_invocation(message, bot_user, text):
        raise SkipHandler()
    replied = getattr(message, "reply_to_message", None)

    # If there is a real question around the name — or a reply/tag target —
    # answer with Noya AI so she can read that message (+ parent reply).
    question = _NOYA_NAME_RE.sub(" ", text).strip(" \t,،:.-")
    question = re.sub(r"\s+", " ", question).strip()
    replied_body = (
        getattr(replied, "text", None) or getattr(replied, "caption", None) or ""
    ).strip()
    replied_has_media = bool(
        replied
        and (
            getattr(replied, "photo", None)
            or getattr(replied, "sticker", None)
            or (
                getattr(replied, "document", None)
                and ((getattr(replied.document, "mime_type", None) or "").startswith("image/"))
            )
        )
    )
    if question or replied_body or replied_has_media:
        if is_other_bot_sender(message, self_bot_id=int(bot_user.id)):
            if not allow_bot_to_bot_reply(int(message.chat.id), int(message.from_user.id)):
                return
        payload = build_noya_user_payload(
            message,
            question,
            bot_username=bot_user.username or "",
        )
        images = await collect_noya_images(message.bot, message)
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        answer, _meta = await run_ai_with_memory(
            message,
            payload,
            call_noya_api,
            session_id=f"telegram:{message.chat.id}",
            images=images or None,
        )
        await _reply_html(message, answer)
        return

    await message.reply(random.choice(RESPONSES))
