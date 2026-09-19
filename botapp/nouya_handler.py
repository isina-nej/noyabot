import random
import re

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent

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

router = Router()

# Persian-aware word boundary around «نویا».
_NOYA_NAME_RE = re.compile(r"(?<![\wآ-ی])نویا(?![\wآ-ی])", re.IGNORECASE)
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


def _channel_question(text: str, bot_username: str = "") -> str | None:
    text = text.strip()
    mention = bool(
        bot_username
        and re.search(rf"@{re.escape(bot_username)}\b", text, flags=re.IGNORECASE)
    )
    name_call = bool(_NOYA_NAME_RE.search(text))
    if not (mention or name_call):
        return None
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    text = _NOYA_NAME_RE.sub("", text)
    return text.strip() or "به این پست پاسخ بده."


@router.channel_post(F.text | F.caption | F.rich_message)
async def channel_nouya_handler(message: types.Message):
    bot_user = await message.bot.get_me()
    body = extract_message_body(message)
    question = _channel_question(body, bot_user.username or "")
    if question is None:
        return
    images = await collect_noya_images(message.bot, message)
    progress = await message.reply("یک لحظه…")
    answer, _meta = await call_noya_api(
        question,
        session_id=f"telegram:channel:{message.chat.id}:{message.message_id}",
        images=images or None,
    )
    try:
        await progress.edit_text(answer, parse_mode="HTML")
    except TelegramBadRequest:
        await progress.edit_text(answer)


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


@router.message(F.text.regexp(_NOYA_NAME_RE))
async def nouya_mention_handler(message: types.Message):
    """Handle bare/mid-sentence «نویا» mentions that the main prefix path missed."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        raise SkipHandler()

    bot_user = await message.bot.get_me()
    if is_self_bot_message(message, self_bot_id=int(bot_user.id)):
        return
    username = (bot_user.username or "").lower()
    if username and f"@{username}" in text.lower():
        raise SkipHandler()

    # Prefix path belongs to handle_text_message.
    if _AI_PREFIX_RE.match(text):
        raise SkipHandler()
    replied = getattr(message, "reply_to_message", None)
    if replied and getattr(replied, "from_user", None) and replied.from_user.id == bot_user.id:
        raise SkipHandler()

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
