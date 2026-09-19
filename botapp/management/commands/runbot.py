import asyncio
import logging
import os
import re
import socket
from collections import OrderedDict
from datetime import time, timedelta
from html import escape
from time import monotonic

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from django.utils import timezone

from botapp.services import (
    add_warning,
    call_ai_api as request_ai_answer,
    call_noya_api,
    generate_noya_image,
    generate_noya_tts,
    edit_noya_image,
    clear_warnings,
    contains_blocked_word,
    consume_group_quota,
    create_moderation_log,
    extract_urls,
    get_active_warning_count,
    get_or_create_moderation_settings,
    is_allowed_url,
    is_duplicate_message,
    is_flooding,
)

from botapp.models import (
    BotMessageSettings,
    ChatLink,
    GroupActionLog,
    GroupSchedule,
    GroupSettings,
)
from botapp.forced_membership import process_destination_member_update
from botapp.forced_membership_handlers import configure_super_admins, router as forced_membership_router
from botapp.forced_membership_runtime import ForcedMembershipMiddleware
from botapp.bot_start_gate import (
    BotStartGateMiddleware,
    OFFICIAL_BOT_USERNAME,
    WELCOME_TEXT,
    clear_notice_record,
    mark_user_started,
    notice_cleanup_loop,
    parse_deep_link_payload,
)
from botapp.moderation import warning_ceiling_spec
from botapp.telegram_moderation import queue_or_execute
from botapp.template_renderer import render_member_template
from botapp.memory.commands import (
    forget_all_command,
    forget_command,
    memories_command,
)
from botapp.memory.integration import run_ai_with_memory
from botapp.noya_context import build_noya_user_payload
from botapp.noya_bot_chat import (
    allow_bot_to_bot_reply,
    is_other_bot_sender,
    is_self_bot_message,
)
from botapp.noya_edits import coordinator as noya_edit_coordinator
from botapp.noya_address import is_addressing_noya
from botapp.telegram_media import collect_noya_images
from botapp.telegram_rich import extract_message_body
from botapp.emoji_handlers import router as emoji_router
from botapp.nouya_handler import router as nouya_router
from botapp.agent_handlers import ArchiveMiddleware, router as agent_router
logger = logging.getLogger(__name__)

TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID", "@CoffeeMan_nej").strip()
ADMIN_IDS = frozenset(
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
)
def parse_bot_tokens(bot_tokens: str = "", bot_token: str = "") -> tuple[str, ...]:
    values = re.split(r"[\s,]+", bot_tokens.strip()) if bot_tokens.strip() else []
    if bot_token.strip():
        values.append(bot_token.strip())
    return tuple(dict.fromkeys(value for value in values if value))


BOT_TOKENS = parse_bot_tokens(os.getenv("BOT_TOKENS", ""), os.getenv("BOT_TOKEN", ""))
COLLAB_DISABLED_TEXT = "ادمین گزینه همکاری را غیر فعال کرده است."
PROMPT_URL_PREFIX = os.getenv("PROMPT_URL_PREFIX", "https://ai.tinkera.org/chat/")
AI_API_URL = os.getenv("AI_API_URL", "https://ai.tinkera.org/api/chat")

router = Router()



@sync_to_async
def get_message_settings():
    settings, _ = BotMessageSettings.objects.get_or_create(pk=1)
    return {
        "start_message": settings.start_message,
        "join_channel_message": settings.join_channel_message,
        "collaboration_enabled": settings.collaboration_enabled,
        "channel_join_required": settings.channel_join_required,
        "bot_start_required": settings.bot_start_required,
    }


MESSAGE_SETTING_FIELDS = {"start_message", "join_channel_message"}
GROUP_SETTING_FIELDS = {
    "collaboration_enabled",
    "channel_join_required",
    "bot_start_required",
    "moderation_enabled",
    "anti_spam_enabled",
    "anti_link_enabled",
    "anti_forward_enabled",
    "welcome_enabled",
    "goodbye_enabled",
    "rules_enabled",
    "captcha_enabled",
    "max_warnings",
    "max_warnings_action",
}


@sync_to_async
def update_message_setting(field_name, value):
    if field_name not in MESSAGE_SETTING_FIELDS:
        raise ValueError(f"Unsupported message setting: {field_name}")
    settings, _ = BotMessageSettings.objects.get_or_create(pk=1)
    setattr(settings, field_name, value)
    settings.save(update_fields=[field_name])
    return value


@sync_to_async
def update_collaboration_setting(enabled: bool):
    settings, _ = BotMessageSettings.objects.get_or_create(pk=1)
    settings.collaboration_enabled = enabled
    settings.save(update_fields=["collaboration_enabled"])
    return enabled


@sync_to_async(thread_sensitive=True)
def get_or_create_group_settings(chat_id: int, chat_title: str = "") -> GroupSettings:
    group, _ = GroupSettings.objects.get_or_create(
        chat_id=chat_id,
        defaults={"chat_title": chat_title},
    )
    if chat_title and group.chat_title != chat_title:
        group.chat_title = chat_title
        group.save(update_fields=["chat_title", "updated_at"])
    return group


@sync_to_async
def update_group_setting(chat_id: int, field_name: str, value):
    if field_name not in GROUP_SETTING_FIELDS:
        raise ValueError(f"Unsupported group setting: {field_name}")
    group = GroupSettings.objects.get(chat_id=chat_id)
    setattr(group, field_name, value)
    group.save(update_fields=[field_name, "updated_at"])
    return value


@sync_to_async
def log_group_action(group: GroupSettings, admin_user_id: int, admin_name: str, action: str, old_value, new_value):
    GroupActionLog.objects.create(
        group=group,
        admin_user_id=admin_user_id,
        admin_name=admin_name,
        action=action,
        old_value=old_value,
        new_value=new_value,
    )


@sync_to_async
def get_group_recent_logs(chat_id: int, limit: int = 5):
    try:
        group = GroupSettings.objects.get(chat_id=chat_id)
        return list(group.logs.all()[:limit])
    except GroupSettings.DoesNotExist:
        return []


@sync_to_async
def create_chat_link(group_chat_id: int, group_title: str, created_by: int) -> ChatLink:
    link = ChatLink.objects.create(
        group_chat_id=group_chat_id,
        group_title=group_title,
        created_by_user_id=created_by,
    )
    return link


def render_template(template: str, user, channel: str):
    first_name = escape(user.first_name or "کاربر")
    mention = f'<a href="tg://user?id={user.id}">{first_name}</a>'
    return (
        template
        .replace("{mention}", mention)
        .replace("{channel}", escape(channel))
        .replace("{name}", first_name)
    )


def collaboration_disabled_message() -> str:
    return COLLAB_DISABLED_TEXT


def get_join_channel_keyboard(channel: str):
    keyboard = [
        [InlineKeyboardButton(text="عضویت در کانال", url=f"https://t.me/{channel.lstrip('@')}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)



def get_group_settings_keyboard(group: GroupSettings) -> InlineKeyboardMarkup:
    def mark(field):
        return "✅" if getattr(group, field) else "❌"

    items = (
        ("moderation", "moderation_enabled", "مدیریت"),
        ("spam", "anti_spam_enabled", "ضداسپم"),
        ("link", "anti_link_enabled", "ضدلینک"),
        ("forward", "anti_forward_enabled", "ضدفوروارد"),
        ("welcome", "welcome_enabled", "خوش‌آمد"),
        ("rules", "rules_enabled", "قوانین"),
        ("collab", "collaboration_enabled", "هوش مصنوعی"),
        ("botstart", "bot_start_required", "استارت ربات"),
    )
    keyboard = [
        [InlineKeyboardButton(
            text=f"{label}: {mark(field)}",
            callback_data=f"toggle:{key}:{group.chat_id}",
        )]
        for key, field, label in items
    ]

    # Max warnings control
    mw = getattr(group, "max_warnings", 3)
    keyboard.append([
        InlineKeyboardButton(text=f"سقف اخطار: {mw}", callback_data=f"maxwarn:show:{group.chat_id}"),
        InlineKeyboardButton(text="−", callback_data=f"maxwarn:-:{group.chat_id}"),
        InlineKeyboardButton(text="+", callback_data=f"maxwarn:+:{group.chat_id}"),
    ])

    # Punishment type (mute or ban)
    punish = getattr(group, "max_warnings_action", "mute")
    punish_label = "سکوت" if punish == "mute" else "بن"
    keyboard.append([
        InlineKeyboardButton(text=f"مجازات در سقف: {punish_label}", callback_data=f"punish:toggle:{group.chat_id}"),
    ])

    keyboard.append([InlineKeyboardButton(
        text="عضویت اجباری",
        callback_data=f"fmr:list:{group.chat_id}",
    )])
    keyboard.append([InlineKeyboardButton(text="تاریخچه", callback_data=f"logs:{group.chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def group_settings_text(group: GroupSettings) -> str:
    punish = getattr(group, "max_warnings_action", "mute")
    punish_label = "سکوت" if punish == "mute" else "بن"
    return (
        f"پنل مدیریت: {group.chat_title or group.chat_id}\n\n"
        f"سقف پیام: {group.flood_limit} پیام در {group.flood_window_seconds} ثانیه\n"
        f"mute خودکار: {group.mute_duration_minutes} دقیقه\n"
        f"سقف اخطار: {group.max_warnings}  (مجازات: {punish_label})\n"
        f"کانال اجباری: {group.mandatory_channel or 'تنظیم نشده'}\n"
        f"دامنه‌های مجاز: {', '.join(group.allowed_domains) or 'هیچ‌کدام'}\n"
        f"کلمات ممنوع: {len(group.blocked_words)} مورد"
    )


async def call_ai_api(
    question: str,
    session_id: str = "telegram-bot",
    *,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
) -> str:
    return await request_ai_answer(
        AI_API_URL,
        question,
        session_id,
        speaker_user_id=speaker_user_id,
        speaker_name=speaker_name,
    )


def prompt_link(token: str) -> str:
    return f"{PROMPT_URL_PREFIX.rstrip('/')}/{token}"


def command_argument(text: str | None) -> str:
    if not text:
        return ""
    _, separator, argument = text.partition(" ")
    return argument.strip() if separator else ""


def user_label(user) -> str:
    return user.full_name or user.username or str(user.id)


def target_from_reply(message: Message):
    return message.reply_to_message.from_user if message.reply_to_message else None


def action_reason(command: CommandObject | None) -> str:
    return (command.args or "").strip() if command else ""


def plain_command(text: str | None) -> tuple[str, str]:
    if not text:
        return "", ""
    command, _, args = text.strip().partition(" ")
    return command.casefold().lstrip("/"), args.strip()


def split_delete_modifier(args: str) -> tuple[str, bool]:
    parts = args.split()
    delete_words = {"delete", "del", "حذف", "پاک"}
    should_delete = any(part.casefold() in delete_words for part in parts)
    clean = " ".join(part for part in parts if part.casefold() not in delete_words)
    return clean, should_delete


def parse_action_arguments(args: str) -> tuple[int, int | None, str]:
    """Parse: [delay] [duration|permanent] [reason]. Example: 60 30 spam."""
    parts = args.split()
    numbers = []
    while parts and len(numbers) < 2:
        token = parts[0].casefold()
        if token in {"permanent", "دائم", "دائمی", "-"}:
            numbers.append(None)
            parts.pop(0)
            continue
        try:
            numbers.append(int(token))
            parts.pop(0)
        except ValueError:
            break
    if not numbers:
        return 0, None, args.strip()
    if len(numbers) == 1:
        return 0, numbers[0], " ".join(parts)
    return numbers[0] or 0, numbers[1], " ".join(parts)


PLAIN_MODERATION_COMMANDS = {
    # === WARN / WARNING / اخطار / هشدار (very complete) ===
    "warn": "warn", "war": "warn", "warning": "warn", "warns": "warns",
    "varn": "warn", "varning": "warn", "varnning": "warn", "varn kon": "warn",
    "akhtar": "warn", "akhbar": "warn", "hoshdar": "warn", "hoshdaar": "warn",
    "hazdar": "warn", "hazdar bede": "warn", "warn kon": "warn", "warn bede": "warn",
    "اخطار": "warn", "اخطاربده": "warn", "اخطار بده": "warn", "اخطار کن": "warn",
    "هشدار": "warn", "هشداربده": "warn", "هشدار بده": "warn", "هشدار کن": "warn",
    "اخطارها": "warns", "هشدارها": "warns", "وارنها": "warns", "اخطار ها": "warns",
    "warn user": "warn", "give warn": "warn", "akhtar bede": "warn", "hoshdar bede": "warn",
    "warn kon": "warn", "warning kon": "warn", "war kon": "warn",

    # === MUTE / سکوت / میوت (full) ===
    "mute": "mute", "miut": "mute", "miute": "mute", "miut kon": "mute",
    "mute kon": "mute", "sokot": "mute", "saket": "mute", "sokoot": "mute",
    "sakoot": "mute", "sokot kon": "mute", "saket kon": "mute",
    "میوت": "mute", "میوت کن": "mute", "سکوت": "mute", "سکوت کن": "mute",
    "ساکت": "mute", "ساکت کن": "mute", "miutkon": "mute", "sokotkon": "mute",
    "mute user": "mute", "miut user": "mute", "sokot kon": "mute",
    "saket kon": "mute", "miut bede": "mute", "sokot bede": "mute",

    # === UNMUTE / رفع سکوت (full) ===
    "unmute": "unmute", "un mute": "unmute", "anmiut": "unmute", "anmute": "unmute",
    "rafemiut": "unmute", "rafesokot": "unmute", "raf saket": "unmute",
    "unmute kon": "unmute", "آنمیوت": "unmute", "رفعمیوت": "unmute",
    "رفع‌میوت": "unmute", "رفع میوت": "unmute", "رفع سکوت": "unmute",
    "رفع ساکت": "unmute", "آن میوت": "unmute", "raf miut": "unmute",
    "unmute user": "unmute", "raf sokot": "unmute", "raf saket kon": "unmute",

    # === BAN / بن / مسدود / بلاک (full) ===
    "ban": "ban", "bon": "ban", "ban kon": "ban", "masdood": "ban",
    "masdod": "ban", "masdud": "ban", "block": "ban", "blok": "ban",
    "ban kon": "ban", "بن": "ban", "بن کن": "ban", "مسدود": "ban",
    "مسدود کن": "ban", "بلاک": "ban", "بلاک کن": "ban", "ban bede": "ban",
    "ban user": "ban", "block user": "ban", "masdood kon": "ban",
    "ban kon user": "ban", "masdod kon": "ban",

    # === UNBAN / رفع بن (full) ===
    "unban": "unban", "un ban": "unban", "anbon": "unban", "rafbon": "unban",
    "rafemasdood": "unban", "raf masdood": "unban", "آنبن": "unban",
    "رفع‌بن": "unban", "رفعبن": "unban", "رفع بن": "unban",
    "رفع مسدود": "unban", "رفع بلاک": "unban", "unban kon": "unban",
    "unban user": "unban", "raf bon": "unban",

    # === LOCK GROUP / قفل گروه (very complete) ===
    "lock": "lock", "lok": "lock", "lock kon": "lock", "lok kon": "lock",
    "ghoofl": "lock", "ghofel": "lock", "ghoofel": "lock", "ghofel kon": "lock",
    "ghoofl kon": "lock", "lock group": "lock", "lok group": "lock",
    "قفل": "lock", "قفل کن": "lock", "قفل گروه": "lock", "قفل‌گروه": "lock",
    "قفل کن گروه": "lock", "lok kon": "lock", "ghofel kon group": "lock",
    "lock kon group": "lock", "ghoofl kon group": "lock", "lok group": "lock",

    # === UNLOCK GROUP / باز کردن گروه (very complete) ===
    "unlock": "unlock", "anlok": "unlock", "unlock kon": "unlock",
    "anlok kon": "unlock", "baz": "unlock", "baz kon": "unlock",
    "baz kard": "unlock", "baz kon group": "unlock", "باز": "unlock",
    "باز کردن": "unlock", "بازکردن": "unlock", "باز کن": "unlock",
    "رفع قفل": "unlock", "رفع‌قفل": "unlock", "رفع قفل کن": "unlock",
    "an lock": "unlock", "unlock group": "unlock", "baz kard group": "unlock",
    "baz kon": "unlock", "raf ghofel": "unlock", "raf qofl": "unlock",

    # === DELETE MESSAGES - extremely comprehensive (delete / del / حذف / پاک) ===
    "delete": "delete", "del": "delete", "delete kon": "delete",
    "del kon": "delete", "hazf": "delete", "hazf kon": "delete",
    "pak": "delete", "pak kon": "delete", "pakkon": "delete",
    "pak kard": "delete", "delete message": "delete", "del message": "delete",
    "حذف": "delete", "حذف کن": "delete", "پاک": "delete", "پاک کن": "delete",
    "پاککردن": "delete", "hazf kon": "delete", "delete kon": "delete",
    "del 100": "delete", "حذف ۱۰۰": "delete", "pak 50": "delete",
    "delete to here": "delete", "حذف تا اینجا": "delete",
    "hazf ta inja": "delete", "del ta inja": "delete", "pak ta inja": "delete",
    "delete until here": "delete", "hazf ta inja": "delete",
    "del until here": "delete", "حذف تا این پیام": "delete",
    "حذف تا اینجا": "delete", "delete ta inja": "delete",
}


def is_group_chat(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def is_private_chat(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def reply_noya_answer(message: Message, answer: str) -> None:
    """Send Noya's reply; prefer HTML so creator text-mentions stay clickable."""
    text = (answer or "").strip() or "…"
    try:
        await message.reply(text, parse_mode="HTML")
    except TelegramBadRequest:
        await message.reply(text)


def is_bot_mentioned(message: Message, bot_username: str) -> bool:
    if not bot_username:
        return False

    if message.reply_to_message:
        replied_user = message.reply_to_message.from_user
        if replied_user and (replied_user.username or "").lower() == bot_username.lower():
            return True

    body = extract_message_body(message)
    if body:
        pattern = rf'@{re.escape(bot_username)}\b'
        if re.search(pattern, body, flags=re.IGNORECASE):
            return True
        # Command mention: /start@OurBot
        if re.search(rf'/\w+@{re.escape(bot_username)}\b', body, flags=re.IGNORECASE):
            return True

    for entity in list(getattr(message, "entities", None) or []) + list(
        getattr(message, "caption_entities", None) or []
    ):
        etype = getattr(entity, "type", None)
        etype_val = getattr(etype, "value", etype)
        if str(etype_val) in {"mention", "text_mention", "bot_command"} and body:
            frag = body[entity.offset : entity.offset + entity.length]
            if bot_username.lower() in frag.lower():
                return True

    return False


def extract_question_from_mention(message: Message, bot_username: str) -> str:
    body = extract_message_body(message)
    if not body:
        return ""

    if not bot_username:
        return body

    pattern = rf'@{re.escape(bot_username)}\s*'
    question = re.sub(pattern, '', body, flags=re.IGNORECASE).strip()

    return question


async def send_moderation_notice(message: Message, text: str):
    try:
        await message.reply(text)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("Could not send moderation notice in chat %s", message.chat.id)


async def punish_for_moderation(message: Message, bot: Bot, group: GroupSettings, reason: str, action: str | None = None):
    user = message.from_user
    if not user or await is_group_admin(message.chat.id, user.id, bot):
        return
    await safe_delete(message)
    action = action or "mute"
    duration = group.mute_duration_minutes
    try:
        if action == "ban":
            await bot.ban_chat_member(chat_id=message.chat.id, user_id=user.id)
        else:
            until_date = timezone.now() + timedelta(minutes=duration)
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception("Moderation action %s failed in chat %s", action, message.chat.id)
    await create_moderation_log(
        group.id,
        action,
        target_user_id=user.id,
        target_name=user_label(user),
        reason=reason,
        duration_minutes=None if action == "ban" else duration,
    )
    await send_moderation_notice(message, f"اقدام مدیریت انجام شد: {reason}")


async def process_moderation(message: Message, bot: Bot, group: GroupSettings) -> bool:
    if not group.moderation_enabled or not message.from_user:
        return False
    user = message.from_user
    # Other bots are not moderated here — Telegram bot-to-bot replies/tags must
    # reach Noya handlers instead of being treated as spam/flood.
    if getattr(user, "is_bot", False):
        return False
    if await is_group_admin(message.chat.id, user.id, bot):
        return False

    text = message.text or message.caption or ""
    if group.anti_forward_enabled and message.forward_origin:
        await punish_for_moderation(message, bot, group, "ارسال پیام فورواردشده مجاز نیست.")
        return True
    if group.anti_spam_enabled:
        if is_flooding(
            message.chat.id,
            user.id,
            group.flood_limit,
            group.flood_window_seconds,
        ):
            await punish_for_moderation(message, bot, group, "ارسال پیام‌های پشت‌سرهم.")
            return True
        if text and is_duplicate_message(
            message.chat.id,
            user.id,
            text,
            group.duplicate_limit,
            group.flood_window_seconds,
        ):
            await punish_for_moderation(message, bot, group, "تکرار پیام یکسان.")
            return True
    if group.anti_link_enabled:
        urls = extract_urls(text)
        if urls and not all(is_allowed_url(url, group.allowed_domains) for url in urls):
            await punish_for_moderation(message, bot, group, "ارسال لینک مجاز نیست.")
            return True
    if contains_blocked_word(text, group.blocked_words):
        await punish_for_moderation(message, bot, group, "پیام شامل کلمه ممنوع است.")
        return True
    return False




async def render_text_with_custom_emojis(text: str) -> str:
    from botapp.models import CustomEmoji
    import re

    # This can be slow. A better approach would be to cache emojis.
    # ponytail: cache emojis
    @sync_to_async
    def get_all_emojis():
        return {emoji.name: emoji.custom_emoji_id for emoji in CustomEmoji.objects.all()}

    emoji_map = await get_all_emojis()

    def replace_emoji_tag(match):
        emoji_name = match.group(1)
        if emoji_id := emoji_map.get(emoji_name):
            return f'<tg-emoji emoji-id="{emoji_id}">✨</tg-emoji>'
        return match.group(0)

    return re.sub(r"<emoji id=([\w-]+)>", replace_emoji_tag, text)


@router.message(Command("start"))
async def send_welcome(message: Message, bot: Bot, command: CommandObject | None = None):
    user = message.from_user
    if not user:
        return

    deep_link_payload = command.args if command else ""
    if deep_link_payload:
        group_id = parse_deep_link_payload(
            deep_link_payload, current_user_id=user.id
        )
        if group_id is not None:
            await mark_user_started(user.id)
            settings = await get_message_settings()
            text = await render_text_with_custom_emojis(settings["start_message"])
            await message.answer(text, parse_mode="HTML")
            return

    settings = await get_message_settings()
    text = await render_text_with_custom_emojis(settings["start_message"])
    await message.answer(text, parse_mode="HTML")
    await mark_user_started(user.id)


@router.message(Command("rules"))
async def show_rules(message: Message):
    if not is_group_chat(message):
        await message.reply("این دستور فقط در گروه قابل استفاده است.")
        return
    group = await get_or_create_moderation_settings(message.chat.id, message.chat.title or "")
    if group.rules_enabled:
        await message.reply(group.rules_text)


@router.message(Command("id"))
async def show_id(message: Message):
    target = target_from_reply(message)
    if target:
        await message.reply(f"شناسه کاربر: {target.id}")
    else:
        await message.reply(f"شناسه این گفتگو: {message.chat.id}\nشناسه شما: {message.from_user.id}")


async def moderation_command_context(message: Message, bot: Bot, protect_target: bool = True):
    if not is_group_chat(message):
        return None
    if not await is_group_admin(message.chat.id, message.from_user.id, bot):
        await message.reply("فقط ادمین‌های گروه می‌توانند این دستور را اجرا کنند.")
        return None
    target = target_from_reply(message)
    if not target:
        await message.reply("این دستور را در پاسخ به پیام کاربر اجرا کنید.")
        return None
    group = await get_or_create_moderation_settings(message.chat.id, message.chat.title or "")
    if protect_target and (
        getattr(target, "is_bot", False)
        or target.id == bot.id
        or await is_group_admin(message.chat.id, target.id, bot)
    ):
        # Remove warnings created by the old buggy behavior.
        await clear_warnings(group.id, target.id)
        await message.reply(f"{user_label(target)} ادمین گروه است و نمی‌توان او را مجازات کرد.")
        return None
    return group, target


async def delete_message_ids(bot, chat_id, message_ids):
    message_ids = sorted({value for value in message_ids if value > 0})
    for start in range(0, len(message_ids), 100):
        chunk = message_ids[start:start + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except (TelegramBadRequest, TelegramForbiddenError):
            for message_id in chunk:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except (TelegramBadRequest, TelegramForbiddenError):
                    continue
    # Best-effort: record that the bot deleted these messages (opt-in archive).
    try:
        from botapp import message_archive

        if message_ids and message_archive.archive_enabled():
            await sync_to_async(message_archive.mark_deleted_by_bot, thread_sensitive=True)(
                chat_id, message_ids, reason="moderation.delete"
            )
    except Exception:  # archival must never break moderation
        logger.debug("snapshot delete-marking failed", exc_info=True)


async def perform_delete(message: Message, bot: Bot, args: str = ""):
    if not is_group_chat(message) or not await is_group_admin(
        message.chat.id,
        message.from_user.id,
        bot,
    ):
        await message.reply("فقط ادمین‌های گروه می‌توانند پیام حذف کنند.")
        return
    normalized = " ".join(args.casefold().split())
    target = message.reply_to_message
    delete_until_variants = {
        "تا اینجا", "تااینجا", "to here", "tohere", "تااین جا",
        "hazf ta inja", "del ta inja", "pak ta inja", "delete to here",
        "hazf ta inja", "del ta inja", "delete ta inja"
    }
    if normalized in delete_until_variants:
        if not target:
            await message.reply("برای «حذف تا اینجا» باید روی پیام مقصد ریپلای کنید.")
            return
        start_id = target.message_id
        end_id = message.message_id
        if end_id - start_id > 1000:
            await message.reply("برای امنیت، حذف تا اینجا حداکثر ۱۰۰۰ پیام است.")
            return
        await delete_message_ids(bot, message.chat.id, range(start_id, end_id + 1))
        return
    if normalized.isdigit():
        count = min(max(int(normalized), 1), 1000)
        start_id = max(1, message.message_id - count)
        await delete_message_ids(bot, message.chat.id, range(start_id, message.message_id + 1))
        return
    if not target:
        await message.reply("روی پیام موردنظر ریپلای کنید یا بنویسید: حذف 100")
        return
    await delete_message_ids(
        bot,
        message.chat.id,
        [target.message_id, message.message_id],
    )


async def perform_scheduled_action(
    message: Message,
    bot: Bot,
    action: str,
    args: str = "",
):
    args, should_delete = split_delete_modifier(args)
    if not is_group_chat(message) or not await is_group_admin(
        message.chat.id,
        message.from_user.id,
        bot,
    ):
        await message.reply("فقط ادمین‌های گروه می‌توانند این دستور را اجرا کنند.")
        return
    group = await get_or_create_moderation_settings(message.chat.id, message.chat.title or "")
    target = None if action in {"lock", "unlock"} else target_from_reply(message)
    if action not in {"lock", "unlock"} and not target:
        await message.reply("این دستور را در پاسخ به پیام کاربر اجرا کنید.")
        return
    if action in {"unlock", "unmute", "unban"}:
        delay, duration, reason = parse_action_arguments(args)
        duration = None
    else:
        delay, duration, reason = parse_action_arguments(args)
    try:
        _, text = await queue_or_execute(
            bot=bot,
            group=group,
            action=action,
            target=target,
            actor=message.from_user,
            reason=reason,
            delay_minutes=delay,
            duration_minutes=duration,
        )
    except (TelegramBadRequest, TelegramForbiddenError, ValueError) as exc:
        await message.reply(f"اجرای دستور ممکن نشد: {exc}")
        return
    response = await message.reply(text)
    if should_delete and message.reply_to_message:
        await delete_message_ids(
            bot,
            message.chat.id,
            [
                message.reply_to_message.message_id,
                message.message_id,
                response.message_id,
            ],
        )


async def perform_warn(message: Message, bot: Bot, reason: str = ""):
    reason, should_delete = split_delete_modifier(reason)
    context = await moderation_command_context(message, bot)
    if not context:
        return
    group, target = context
    count = await add_warning(
        group.id,
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
        reason,
        group.warning_expiry_days,
    )
    await create_moderation_log(
        group.id,
        "warn",
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
        reason,
    )
    if count < group.max_warnings:
        response = await message.reply(
            f"به {user_label(target)} اخطار داده شد ({count}/{group.max_warnings})."
        )
        if should_delete and message.reply_to_message:
            await delete_message_ids(
                bot,
                message.chat.id,
                [
                    message.reply_to_message.message_id,
                    message.message_id,
                    response.message_id,
                ],
            )
        return

    # Reached ceiling — queue/execute the single configured punishment policy.
    spec = warning_ceiling_spec(group)
    try:
        _, result_text = await queue_or_execute(
            bot=bot,
            group=group,
            action=spec.action,
            target=target,
            actor=message.from_user,
            reason="رسیدن به سقف اخطارها",
            delay_minutes=spec.delay_minutes,
            duration_minutes=spec.duration_minutes,
        )
    except (TelegramBadRequest, TelegramForbiddenError, ValueError) as exc:
        await message.reply(
            f"اخطار ثبت شد ({group.max_warnings}/{group.max_warnings})، اما مجازات ثبت نشد: {exc}"
        )
        return
    await clear_warnings(group.id, target.id)
    response = await message.reply(
        f"{user_label(target)} به سقف {group.max_warnings} اخطار رسید؛ "
        f"{result_text} شمارنده اخطار صفر شد."
    )
    if should_delete and message.reply_to_message:
        await delete_message_ids(
            bot,
            message.chat.id,
            [
                message.reply_to_message.message_id,
                message.message_id,
                response.message_id,
            ],
        )


async def perform_mute(message: Message, bot: Bot, reason: str = ""):
    context = await moderation_command_context(message, bot)
    if not context:
        return
    group, target = context
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=timezone.now() + timedelta(minutes=group.mute_duration_minutes),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("ربات دسترسی محدودکردن این کاربر را ندارد.")
        return
    await create_moderation_log(
        group.id,
        "mute",
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
        reason,
        group.mute_duration_minutes,
    )
    await message.reply(f"{user_label(target)} به‌مدت {group.mute_duration_minutes} دقیقه mute شد.")


async def perform_unmute(message: Message, bot: Bot):
    context = await moderation_command_context(message, bot, protect_target=False)
    if not context:
        return
    group, target = context
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
            ),
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("رفع محدودیت ممکن نشد.")
        return
    await create_moderation_log(
        group.id,
        "unmute",
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
    )
    await message.reply(f"محدودیت {user_label(target)} برداشته شد.")


async def perform_ban(message: Message, bot: Bot, reason: str = ""):
    context = await moderation_command_context(message, bot)
    if not context:
        return
    group, target = context
    try:
        await bot.ban_chat_member(chat_id=message.chat.id, user_id=target.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("ربات دسترسی ban کردن این کاربر را ندارد.")
        return
    await create_moderation_log(
        group.id,
        "ban",
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
        reason,
    )
    await message.reply(f"{user_label(target)} از گروه ban شد.")


async def perform_unban(message: Message, bot: Bot):
    context = await moderation_command_context(message, bot, protect_target=False)
    if not context:
        return
    group, target = context
    try:
        await bot.unban_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            only_if_banned=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.reply("رفع ban ممکن نشد.")
        return
    await create_moderation_log(
        group.id,
        "unban",
        target.id,
        user_label(target),
        message.from_user.id,
        user_label(message.from_user),
    )
    await message.reply(f"ban کاربر {user_label(target)} برداشته شد.")


async def perform_show_warnings(message: Message, bot: Bot):
    if not is_group_chat(message):
        return
    if not await is_group_admin(message.chat.id, message.from_user.id, bot):
        await message.reply("فقط ادمین‌های گروه می‌توانند این دستور را اجرا کنند.")
        return
    target = target_from_reply(message) or message.from_user
    group = await get_or_create_moderation_settings(message.chat.id, message.chat.title or "")
    count = await get_active_warning_count(group.id, target.id)
    await message.reply(f"تعداد اخطار فعال {user_label(target)}: {count}")


@router.message(Command("warn"))
async def warn_user(message: Message, command: CommandObject, bot: Bot):
    await perform_warn(message, bot, action_reason(command))


@router.message(Command("delete", "del"))
async def delete_messages_command(message: Message, command: CommandObject, bot: Bot):
    await perform_delete(message, bot, action_reason(command))


@router.message(Command("mute"))
async def mute_user(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "mute", action_reason(command))


@router.message(Command("unmute"))
async def unmute_user(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "unmute", action_reason(command))


@router.message(Command("ban"))
async def ban_user(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "ban", action_reason(command))


@router.message(Command("unban"))
async def unban_user(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "unban", action_reason(command))


@router.message(Command("lock"))
async def lock_group(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "lock", action_reason(command))


@router.message(Command("unlock"))
async def unlock_group(message: Message, command: CommandObject, bot: Bot):
    await perform_scheduled_action(message, bot, "unlock", action_reason(command))


@router.message(Command("warns"))
async def show_warnings(message: Message, bot: Bot):
    await perform_show_warnings(message, bot)


@sync_to_async(thread_sensitive=True)
def set_daily_group_schedule(group_id, action, hour, minute, actor_id):
    schedule, _ = GroupSchedule.objects.update_or_create(
        group_id=group_id,
        action=action,
        defaults={
            "time_of_day": time(hour=hour, minute=minute),
            "is_active": True,
            "last_enqueued_date": None,
            "created_by_user_id": actor_id,
        },
    )
    return schedule


@sync_to_async(thread_sensitive=True)
def delete_daily_group_schedule(group_id, action):
    return GroupSchedule.objects.filter(group_id=group_id, action=action).delete()[0]


@router.message(Command("lockat"))
async def set_lock_time(message: Message, command: CommandObject, bot: Bot):
    await set_group_schedule_command(message, command, bot, "lock")


@router.message(Command("unlockat"))
async def set_unlock_time(message: Message, command: CommandObject, bot: Bot):
    await set_group_schedule_command(message, command, bot, "unlock")


async def set_group_schedule_command(message, command, bot, action):
    if not is_group_chat(message) or not await is_group_admin(message.chat.id, message.from_user.id, bot):
        await message.reply("فقط ادمین‌های گروه می‌توانند زمان‌بندی را تغییر دهند.")
        return
    value = action_reason(command).strip().casefold()
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    if value in {"off", "خاموش", "حذف"}:
        await delete_daily_group_schedule(group.id, action)
        await message.reply("زمان‌بندی حذف شد.")
        return
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except ValueError:
        await message.reply("زمان معتبر نیست. نمونه: /lockat 23:30 یا /lockat off")
        return
    await set_daily_group_schedule(group.id, action, hour, minute, message.from_user.id)
    label = "قفل" if action == "lock" else "بازکردن"
    await message.reply(f"{label} روزانه گروه برای ساعت {hour:02d}:{minute:02d} تنظیم شد.")


@router.message(Command("setrules"))
async def set_rules(message: Message, command: CommandObject, bot: Bot):
    if not is_group_chat(message) or not await is_group_admin(message.chat.id, message.from_user.id, bot):
        return
    text = action_reason(command)
    if not text:
        await message.reply("استفاده: /setrules متن قوانین")
        return
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    group.rules_text = text
    await sync_to_async(group.save)(update_fields=["rules_text", "updated_at"])
    await message.reply("قوانین گروه ذخیره شد.")


@router.message(Command("setwelcome"))
async def set_welcome(message: Message, command: CommandObject, bot: Bot):
    if not is_group_chat(message) or not await is_group_admin(message.chat.id, message.from_user.id, bot):
        return
    text = action_reason(command)
    if not text:
        await message.reply(
            "استفاده: /setwelcome متن خوش‌آمد؛ متغیرها: "
            "#name #title #time #date #datesh"
        )
        return
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    group.welcome_message = text
    await sync_to_async(group.save)(update_fields=["welcome_message", "updated_at"])
    await message.reply("پیام خوش‌آمد ذخیره شد.")


@router.message(Command("filter"))
async def manage_word_filter(message: Message, command: CommandObject, bot: Bot):
    if not is_group_chat(message) or not await is_group_admin(message.chat.id, message.from_user.id, bot):
        return
    args = action_reason(command).split(maxsplit=1)
    if len(args) != 2 or args[0] not in {"add", "del"}:
        await message.reply("استفاده: /filter add کلمه یا /filter del کلمه")
        return
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    word = args[1].strip()
    words = list(group.blocked_words)
    if args[0] == "add" and word not in words:
        words.append(word)
    elif args[0] == "del":
        words = [value for value in words if value != word]
    group.blocked_words = words
    await sync_to_async(group.save)(update_fields=["blocked_words", "updated_at"])
    await message.reply(f"فیلتر کلمات به‌روزرسانی شد؛ {len(words)} مورد فعال است.")


@router.message(Command("allowdomain"))
async def manage_allowed_domain(message: Message, command: CommandObject, bot: Bot):
    if not is_group_chat(message) or not await is_group_admin(message.chat.id, message.from_user.id, bot):
        return
    args = action_reason(command).split(maxsplit=1)
    if len(args) != 2 or args[0] not in {"add", "del"}:
        await message.reply("استفاده: /allowdomain add example.com یا /allowdomain del example.com")
        return
    domain = args[1].lower().strip().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
    if not domain or " " in domain:
        await message.reply("دامنه معتبر نیست.")
        return
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    domains = list(group.allowed_domains)
    if args[0] == "add" and domain not in domains:
        domains.append(domain)
    elif args[0] == "del":
        domains = [value for value in domains if value != domain]
    group.allowed_domains = domains
    await sync_to_async(group.save)(update_fields=["allowed_domains", "updated_at"])
    await message.reply(f"دامنه‌های مجاز به‌روزرسانی شد؛ {len(domains)} مورد فعال است.")


@router.message(Command("memories"))
async def memories(message: Message):
    await memories_command(message)


@router.message(Command("forget"))
async def forget_memory(message: Message, command: CommandObject):
    await forget_command(message, action_reason(command))


@router.message(Command("forget_all"))
async def forget_all_memory(message: Message, command: CommandObject):
    await forget_all_command(message, action_reason(command))


@router.message(Command("prompt"))
async def prompt(message: Message, command: CommandObject):
    chat = message.chat

    question = action_reason(command)
    if not question:
        await message.reply("استفاده: /prompt سوال شما")
        return

    if chat.type == ChatType.PRIVATE:
        await message.bot.send_chat_action(chat_id=chat.id, action="typing")
        answer, _meta = await run_ai_with_memory(
            message,
            question,
            call_noya_api,
            session_id=f"telegram:{chat.id}",
        )
        await reply_noya_answer(message, answer)
        return

    # Group /prompt must honor the same collaboration + daily quota gates as @mention.
    group = await get_or_create_group_settings(chat.id, chat.title or "")
    if not group.collaboration_enabled:
        await message.reply("همکاری هوش مصنوعی در این گروه غیرفعال است.")
        return
    if not await consume_group_quota(chat.id, chat.title or ""):
        await message.reply(
            f"گروه {chat.title or chat.id}، به پایان درخواست‌های روزانه خود رسیده است. با مدیریت آن تماس بگیرید."
        )
        return

    await message.bot.send_chat_action(chat_id=chat.id, action="typing")
    answer, _meta = await run_ai_with_memory(
        message,
        question,
        call_noya_api,
        session_id=f"telegram:{chat.id}",
    )
    await reply_noya_answer(message, answer)


_IMAGE_TRIGGER_RE = re.compile(
    r"^(?:لطفاً\s+|لطفا\s+)?(?:برام\s+|واسم\s+)?(?:یه\s+|یک\s+)?(?:عکس|تصویر|نقاشی)\s+(?:از\s+)?(.+?)\s*(?:بکش|بساز|تولید کن|درست کن|بکشی|بکشش|بسازش|بسازی)[\s!؟?.]*$"
    r"|^(?:عکس|تصویر|نقاشی)\s+(?:از\s+)?(.+?)\s*(?:بکش|بساز|تولید کن|درست کن|بکشی|بکشش|بسازش|بسازی)[\s!؟?.]*$"
    r"|^(?:draw|generate|paint|image\s+of)\s+(.+)[\s!؟?.]*$",
    re.IGNORECASE,
)


def extract_image_prompt(text: str) -> str | None:
    t = (text or "").strip()
    match = _IMAGE_TRIGGER_RE.match(t)
    if match:
        for g in match.groups():
            if g and g.strip():
                return g.strip()
    return None


_IMAGE_EDIT_KEYWORDS = (
    "رنگ", "تغییر", "عوض", "ویرایش", "ادیت", "حذف", "اضافه", "بزرگ", "کوچک",
    "روشن", "تاریک", "بچرخون", "برعکس", "سیاه و سفید", "بلور", "محو", "شارپ",
    "کراپ", "برش", "فیلتر", "پس‌زمینه", "بکگراند", "سفید", "سیاه", "قرمز",
    "آبی", "سبز", "زرد", "صورتی", "بنفش", "نارنجی", "طلایی", "خاکستری",
    "edit", "change", "remove", "add", "blur", "crop", "rotate", "flip",
    "کن", "بکن", "بده", "بزن",
)


def _is_image_edit_request(text: str, has_reply_image: bool) -> bool:
    """Detect if user is asking to edit/modify the replied image."""
    if not has_reply_image:
        return False
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(kw in t for kw in _IMAGE_EDIT_KEYWORDS)


async def _handle_noya_image_generation(message: Message, prompt_text: str):
    prompt_clean = (prompt_text or "").strip()
    if not prompt_clean:
        await message.reply("لطفاً توصیف عکسی که می‌خوای رو بگو؛ مثلاً: یک گربه سفید روی مبل")
        return
    if message.bot:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    progress = await message.reply("در حال کشیدن نقاشی برات… 🎨")
    image_bytes = await generate_noya_image(prompt_clean)
    if not image_bytes:
        await progress.edit_text("متأسفانه نتونستم این عکس رو بسازم. لطفاً دوباره با یه توصیف دیگه امتحان کن.")
        return
    try:
        photo = BufferedInputFile(image_bytes, filename="noya.png")
        caption = f"اینم نقاشیت! 🎨\n<blockquote>{escape(prompt_clean[:120])}</blockquote>"
        await message.reply_photo(photo=photo, caption=caption, parse_mode="HTML")
        try:
            await progress.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("Failed sending generated photo to chat=%s", message.chat.id)
        await progress.edit_text("عکس ساخته شد ولی در ارسال تلگرام خطایی پیش اومد.")


async def _handle_noya_image_edit(message: Message, instruction: str, image_data: bytes, image_mime: str = "image/jpeg"):
    """Handle image edit: user replies to an image with edit instructions."""
    if message.bot:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    progress = await message.reply("در حال ویرایش عکس… ✏️")
    edited_bytes = await edit_noya_image(instruction, image_data, image_mime)
    if not edited_bytes:
        await progress.edit_text("متأسفانه نتونستم عکس رو ویرایش کنم. لطفاً دوباره امتحان کن.")
        return
    try:
        photo = BufferedInputFile(edited_bytes, filename="noya_edit.png")
        caption = f"بفرما! ✏️\n<blockquote>{escape(instruction[:120])}</blockquote>"
        await message.reply_photo(photo=photo, caption=caption, parse_mode="HTML")
        try:
            await progress.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("Failed sending edited photo to chat=%s", message.chat.id)
        await progress.edit_text("عکس ویرایش شد ولی در ارسال خطا پیش اومد.")


@router.message(Command("draw", "image", "pic", "عکس"))
async def draw_command(message: Message, command: CommandObject):
    prompt_text = action_reason(command)
    if not prompt_text:
        await message.reply("استفاده: /draw توصیف عکس\nمثلاً: /draw یک گربه سفید بامزه روی مبل")
        return
    await _handle_noya_image_generation(message, prompt_text)


# ---------- TTS (Voice) ----------

_VOICE_TRIGGER_RE = re.compile(
    # «ویس بگو X» / «با صدا بخون X» / «صوتی X» / «ویس X»
    r"^(?:لطفاً\s+|لطفا\s+)?(?:با\s+)?(?:صدا|صوت|صوتی|ویس|ویسی|وویس|voice)\s+(?:بگو|بخون|بده|بفرست|بنویس)\s+(.+)"
    # «ویس/صدا/voice : X» or «ویس X» (keyword then text, optional colon)
    r"|^(?:لطفاً\s+|لطفا\s+)?(?:با\s+)?(?:صدا|صوت|صوتی|ویس|ویسی|وویس|voice)\s*[:،\-]\s*(.+)"
    # «X رو ویس بده» / «X رو ویس کن»
    r"|^(.+?)\s+(?:رو\s+)?(?:ویس|صوتی|voice)\s*(?:کن|بده|بفرست|بگو|بخون)[!؟?\s]*$",
    re.IGNORECASE,
)


def extract_voice_text(text: str) -> str | None:
    """Return the text to be spoken if user is requesting TTS; else None."""
    t = (text or "").strip()
    match = _VOICE_TRIGGER_RE.match(t)
    if match:
        for g in match.groups():
            if g and g.strip():
                return g.strip()
    return None


async def _handle_noya_tts(message: Message, text: str, *, as_noya: bool = True) -> None:
    """Generate TTS and send as voice message."""
    text_clean = (text or "").strip()
    if not text_clean:
        await message.reply("لطفاً متنی رو که می‌خوای با صدا بگم بنویس.")
        return
    if message.bot:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="record_voice")
    progress = await message.reply("دارم ضبط می‌کنم… 🎙️")
    audio_bytes = await generate_noya_tts(text_clean, as_noya=as_noya)
    if not audio_bytes:
        await progress.edit_text("متأسفانه نتونستم ویس بسازم. لطفاً دوباره امتحان کن.")
        return
    try:
        voice_file = BufferedInputFile(audio_bytes, filename="noya_voice.mp3")
        caption = f"🎙️ <blockquote>{escape(text_clean[:120])}</blockquote>"
        await message.reply_voice(voice=voice_file, caption=caption, parse_mode="HTML")
        try:
            await progress.delete()
        except Exception:
            pass
    except Exception:
        logger.exception("Failed sending TTS voice to chat=%s", message.chat.id)
        await progress.edit_text("ویس ساخته شد ولی در ارسال خطایی پیش اومد.")


@router.message(Command("voice", "ویس", "صدا", "tts"))
async def voice_command(message: Message, command: CommandObject):
    text = action_reason(command)
    if not text:
        await message.reply("استفاده: /voice متن\nمثلاً: /voice سلام! حالت خوبه؟")
        return
    await _handle_noya_tts(message, text)


# --- Agent ACTION dispatcher (AI decides which tool to use) ---

_ACTION_RE = re.compile(
    r"^\s*\[ACTION:(tts|image|image_edit)\]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


async def _dispatch_ai_action(
    message: Message,
    answer: str,
    *,
    images: list[dict] | None = None,
) -> bool:
    """Parse AI response for [ACTION:xxx] tags and execute.

    Returns True if an action was handled (caller should NOT send text reply).
    """
    m = _ACTION_RE.match((answer or "").strip())
    if not m:
        return False

    action = m.group(1).lower()
    content = m.group(2).strip()

    if action == "tts":
        logger.info("[NOYA-AGENT] 🔊 AI chose TTS: %r", content[:60])
        await _handle_noya_tts(message, content)
        return True

    if action == "image":
        logger.info("[NOYA-AGENT] 🖼️ AI chose image gen: %r", content[:60])
        await _handle_noya_image_generation(message, content)
        return True

    if action == "image_edit":
        if images:
            reply_img = next(
                (i for i in images if i.get("source") in ("reply", "reply_parent")),
                None,
            )
            if reply_img:
                logger.info("[NOYA-AGENT] ✏️ AI chose image edit: %r", content[:60])
                await _handle_noya_image_edit(
                    message, content, reply_img["data"],
                    reply_img.get("mime", "image/jpeg"),
                )
                return True
        # Fallback: no image available for edit, send text
        logger.warning("[NOYA-AGENT] image_edit requested but no reply image")
        return False

    return False


@router.message(Command("new"))
async def new_chat(message: Message, bot: Bot):
    chat = message.chat
    user = message.from_user

    if chat.type == ChatType.PRIVATE:
        await message.reply("باید در گروهی که ربات ادمینه ارسال کنید.")
        return

    if not await is_group_admin(chat.id, user.id, bot):
        await message.reply("فقط ادمین‌ها می‌توانند لینک بسازند.")
        return

    link = await create_chat_link(chat.id, chat.title or "", user.id)

    try:
        await bot.send_message(
            chat_id=user.id,
            text=f"لینک گفت‌وگوی اختصاصی شما:\n\n{prompt_link(link.token)}",
        )
        await message.reply("لینک گفت‌وگو در پیوی ارسال شد.")
    except TelegramForbiddenError:
        await message.reply("لطفا ابتدا ربات را در پیوی استارت کنید تا لینک برایتان ارسال شود.")


@router.message(Command("setcollab"))
async def set_collaboration(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:] if message.text else []
    value = " ".join(args).strip().lower()
    if value in {"on", "1", "true", "enable", "enabled", "روشن", "فعال"}:
        await update_collaboration_setting(True)
        await message.reply("همکاری فعال شد.")
        return
    if value in {"off", "0", "false", "disable", "disabled", "خاموش", "غیرفعال"}:
        await update_collaboration_setting(False)
        await message.reply("همکاری غیرفعال شد.")
        return
    await message.reply("استفاده: /setcollab on یا /setcollab off")


@router.message(Command("settings"))
async def show_settings(message: Message):
    if not is_admin(message.from_user.id):
        return
    settings = await get_message_settings()
    collab = "✅ فعال" if settings["collaboration_enabled"] else "❌ غیرفعال"
    channel = "✅ فعال" if settings["channel_join_required"] else "❌ غیرفعال"
    botstart = "✅ فعال" if settings["bot_start_required"] else "❌ غیرفعال"
    text = (
        "⚙️ تنظیمات ربات:\n\n"
        f"🤝 همکاری: {collab}\n"
        f"📢 عضویت کانال: {channel}\n"
        f"🚀 استارت ربات: {botstart}\n\n"
        "دستورات:\n"
        "/setcollab on|off — فعال/غیرفعال کردن همکاری\n"
        "/togglechannel — تغییر وضعیت عضویت اجباری کانال\n"
        "/togglebotstart — تغییر وضعیت استارت اجباری ربات"
    )
    await message.reply(text)


@router.message(Command("setstartmsg"))
async def set_start_message(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:] if message.text else []
    text = " ".join(args).strip()
    if not text:
        await message.reply("استفاده: /setstartmsg متن پیام استارت")
        return
    await update_message_setting("start_message", text)
    await message.reply("پیام استارت ذخیره شد.")


@router.message(Command("setjoinmsg"))
async def set_join_message(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:] if message.text else []
    text = " ".join(args).strip()
    if not text:
        await message.reply("استفاده: /setjoinmsg متن پیام عضویت اجباری")
        return
    await update_message_setting("join_channel_message", text)
    await message.reply("پیام عضویت اجباری ذخیره شد.")


@router.message(Command("viewmsgs"))
async def view_messages(message: Message):
    if not is_admin(message.from_user.id):
        return
    settings = await get_message_settings()
    text = (
        "پیام‌های فعلی:\n\n"
        f"start_message:\n{settings['start_message']}\n\n"
        f"join_channel_message:\n{settings['join_channel_message']}\n\n"
        f"collaboration_enabled:\n{settings['collaboration_enabled']}\n\n"
        f"channel_join_required:\n{settings['channel_join_required']}\n\n"
        f"bot_start_required:\n{settings['bot_start_required']}\n\n"
        "placeholderها: {mention} {channel} {name}"
    )
    await message.reply(text)


@router.message(F.text.regexp(r'(?i)\b(پنل|تنظیمات)\b'))
async def handle_panel_trigger(message: Message, bot: Bot):
    if message.chat.type == ChatType.PRIVATE:
        return

    chat = message.chat
    user = message.from_user

    if not await is_group_admin(chat.id, user.id, bot):
        return

    group = await get_or_create_group_settings(chat.id, chat.title or "")

    text = group_settings_text(group)
    keyboard = get_group_settings_keyboard(group)

    await message.reply(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("toggle:"))
async def handle_toggle_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    user = callback.from_user
    data = callback.data

    parts = data.split(":")
    if len(parts) != 3:
        return
    _, setting, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return

    if not await is_group_admin(chat_id, user.id, bot):
        await callback.answer("شما ادمین این گروه نیستید.", show_alert=True)
        return

    group = await get_or_create_group_settings(chat_id)

    field_map = {
        "collab": "collaboration_enabled",
        "botstart": "bot_start_required",
        "moderation": "moderation_enabled",
        "spam": "anti_spam_enabled",
        "link": "anti_link_enabled",
        "forward": "anti_forward_enabled",
        "welcome": "welcome_enabled",
        "rules": "rules_enabled",
    }

    field_name = field_map.get(setting)
    if not field_name:
        return

    old_value = getattr(group, field_name)
    new_value = not old_value

    await update_group_setting(chat_id, field_name, new_value)

    admin_name = user.first_name or str(user.id)
    await log_group_action(group, user.id, admin_name, f"toggle_{setting}", old_value, new_value)

    group = await get_or_create_group_settings(chat_id)
    text = group_settings_text(group)
    keyboard = get_group_settings_keyboard(group)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("maxwarn:"))
async def handle_maxwarn_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    user = callback.from_user
    data = callback.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    _, action, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return

    if not await is_group_admin(chat_id, user.id, bot):
        await callback.answer("شما ادمین این گروه نیستید.", show_alert=True)
        return

    group = await get_or_create_group_settings(chat_id)

    current = getattr(group, "max_warnings", 3)
    if action == "+":
        new_val = min(current + 1, 20)
    elif action == "-":
        new_val = max(current - 1, 1)
    elif action == "show":
        new_val = current
    else:
        return

    if new_val != current:
        await update_group_setting(chat_id, "max_warnings", new_val)
        admin_name = user.first_name or str(user.id)
        await log_group_action(group, user.id, admin_name, f"max_warnings", current, new_val)
        group = await get_or_create_group_settings(chat_id)

    text = group_settings_text(group)
    keyboard = get_group_settings_keyboard(group)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("punish:"))
async def handle_punish_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    user = callback.from_user
    data = callback.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    _, action, chat_id_str = parts
    try:
        chat_id = int(chat_id_str)
    except ValueError:
        return

    if not await is_group_admin(chat_id, user.id, bot):
        await callback.answer("شما ادمین این گروه نیستید.", show_alert=True)
        return

    group = await get_or_create_group_settings(chat_id)

    if action == "toggle":
        current = getattr(group, "max_warnings_action", "mute")
        new_val = "ban" if current == "mute" else "mute"
        await update_group_setting(chat_id, "max_warnings_action", new_val)
        admin_name = user.first_name or str(user.id)
        await log_group_action(group, user.id, admin_name, "max_warnings_action", current, new_val)
        group = await get_or_create_group_settings(chat_id)

    text = group_settings_text(group)
    keyboard = get_group_settings_keyboard(group)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("logs:"))
async def handle_logs_callback(callback: CallbackQuery, bot: Bot):
    await callback.answer()

    user = callback.from_user
    data = callback.data
    chat_id = int(data.split(":")[1])

    if not await is_group_admin(chat_id, user.id, bot):
        await callback.answer("شما ادمین این گروه نیستید.", show_alert=True)
        return

    logs = await get_group_recent_logs(chat_id, limit=5)

    if not logs:
        log_text = "هیچ تاریخچه‌ای ثبت نشده است."
    else:
        log_lines = []
        for log in logs:
            time_str = log.timestamp.strftime("%Y-%m-%d %H:%M")
            status = "فعال" if log.new_value else "غیرفعال"
            log_lines.append(f"{time_str} | {log.admin_name}: {log.action} → {status}")
        log_text = "\n".join(log_lines)

    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=f"📜 آخرین ۵ اقدام:\n\n{log_text}"
    )


@sync_to_async
def is_group_admin_db(chat_id: int, user_id: int) -> bool:
    try:
        group = GroupSettings.objects.get(chat_id=chat_id)
        return user_id in group.group_admins
    except GroupSettings.DoesNotExist:
        return False


async def is_group_admin(chat_id: int, user_id: int, bot: Bot) -> bool:
    if user_id in ADMIN_IDS:
        return True

    if await is_group_admin_db(chat_id, user_id):
        return True

    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        return any(admin.user.id == user_id for admin in admins)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception("Could not fetch administrators for chat %s", chat_id)
        return False


_NOYA_PREFIXES = ("نویا", "noya", "nuya", "noia", "nuia")
_NOYA_NAME_RE = re.compile(r"(?<![\wآ-ی])نویا(?![\wآ-ی])", re.IGNORECASE)


def _noya_question_from_text(text: str, bot_username: str = "") -> str | None:
    """Return question text if this message is addressing Noya; else None."""
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    for prefix in _NOYA_PREFIXES:
        if lowered.startswith(prefix):
            rest = raw[len(prefix):].lstrip(" \t,،:.-")
            return rest or "سلام"
    if bot_username:
        mention = f"@{bot_username}".lower()
        if lowered.startswith(mention):
            rest = raw[len(mention):].lstrip(" \t,،:.-")
            return rest or "سلام"
    # Mid-sentence name call: «سازنده‌ات کیه نویا»
    if _NOYA_NAME_RE.search(raw):
        rest = _NOYA_NAME_RE.sub(" ", raw)
        rest = re.sub(r"\s+", " ", rest).strip(" \t,،:.-")
        return rest or "سلام"
    return None


async def _answer_noya_chat(message: Message, question: str, *, use_quota: bool) -> None:
    bot_username = ""
    self_bot_id = None
    try:
        me = await message.bot.me()
        bot_username = me.username or ""
        self_bot_id = int(me.id)
    except Exception:
        bot_username = ""
        self_bot_id = None

    # Never answer our own messages (loop guard).
    if is_self_bot_message(message, self_bot_id=self_bot_id):
        return

    # Other bots: only when they address us (caller already checked triggers),
    # and only within a tight rate limit to stop bot↔bot ping-pong.
    if is_other_bot_sender(message, self_bot_id=self_bot_id):
        sender_id = int(message.from_user.id)
        if not allow_bot_to_bot_reply(int(message.chat.id), sender_id):
            logger.warning(
                "Noya bot-to-bot rate-limited chat=%s sender_bot=%s",
                message.chat.id,
                sender_id,
            )
            return

    if use_quota:
        if not await consume_group_quota(message.chat.id, message.chat.title or ""):
            await message.reply(
                f"گروه {message.chat.title or message.chat.id}، به پایان درخواست‌های روزانه خود رسیده است. با مدیریت آن تماس بگیرید."
            )
            return
    images = []
    try:
        images = await collect_noya_images(message.bot, message)
    except Exception:
        logger.exception("Failed collecting Noya vision images chat=%s", message.chat.id)
        images = []
    ask = (question or "").strip()
    t_chat_start = monotonic()
    logger.info(
        "[NOYA-TIMING] 📩 Incoming chat msg_id=%s chat_id=%s from_user=%s: %r",
        message.message_id,
        message.chat.id,
        getattr(message.from_user, "id", None),
        ask[:60],
    )
    if images and not ask:
        ask = "این تصویر / استیکر را ببین و پاسخ بده."
    # Attach reply-chain + recent group chatter so Noya can read the chat/tag target.
    payload = build_noya_user_payload(message, ask, bot_username=bot_username)
    if is_other_bot_sender(message, self_bot_id=self_bot_id):
        sender = message.from_user
        bot_label = (getattr(sender, "username", None) or getattr(sender, "full_name", None) or str(sender.id))
        payload = (
            f"[SPEAKER_BOT]\n"
            f"telegram_bot_id={int(sender.id)}\n"
            f"username={bot_label}\n"
            f"note=این پیام از یک ربات دیگر است؛ اگر ریپلای/تگ کرده جواب بده."
            f"\n[/SPEAKER_BOT]\n\n{payload}"
        )
    img_prompt = extract_image_prompt(ask)
    if img_prompt and not images:
        await _handle_noya_image_generation(message, img_prompt)
        return
    # Image edit: user replies to an image with edit instructions
    reply_has_image = bool(images and any(i.get("source") in ("reply", "reply_parent") for i in images))
    if reply_has_image and _is_image_edit_request(ask, True):
        # Get the first reply image for editing
        reply_img = next((i for i in images if i.get("source") in ("reply", "reply_parent")), None)
        if reply_img:
            logger.info("[NOYA-TIMING] ✏️ Image edit detected: %r", ask[:60])
            await _handle_noya_image_edit(message, ask, reply_img["data"], reply_img.get("mime", "image/jpeg"))
            return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    answer, agent_metadata = await run_ai_with_memory(
        message,
        payload,
        call_noya_api,
        session_id=f"telegram:{message.chat.id}",
        images=images or None,
    )
    t_reply = monotonic()
    # --- Agent tool results (image/TTS from agent loop) ---
    if agent_metadata.get("generated_image_b64"):
        import base64
        img_bytes = base64.b64decode(agent_metadata["generated_image_b64"])
        await message.reply_photo(BufferedInputFile(img_bytes, filename="noya_image.png"), caption=answer or "")
    elif agent_metadata.get("generated_tts_audio"):
        audio_bytes = agent_metadata["generated_tts_audio"]
        voice_file = BufferedInputFile(audio_bytes, filename="noya_voice.mp3")
        await message.reply_voice(voice=voice_file)
        clean_answer = (answer or "").replace("صدا آماده شد.", "").strip()
        if clean_answer:
            await reply_noya_answer(message, clean_answer)
    else:
        # --- Legacy ACTION dispatch (fallback for non-agent mode) ---
        handled = await _dispatch_ai_action(message, answer or "", images=images)
        if not handled:
            await reply_noya_answer(message, answer)
    logger.info(
        "[NOYA-TIMING] 📤 Telegram reply finished for msg_id=%s in %.1fms (total_turn=%.1fms)",
        message.message_id,
        (monotonic() - t_reply) * 1000,
        (monotonic() - t_chat_start) * 1000,
    )


async def _handle_noya_text_message(message: Message, bot: Bot, *, allow_commands: bool) -> None:
    """Shared path for new + edited text that addresses Noya."""
    body = _message_body(message)
    if not body:
        return

    bot_info = await bot.me()
    bot_username = bot_info.username or ""

    # Private chat: any non-command text is a conversation with Noya.
    if is_private_chat(message):
        if body.startswith("/"):
            return
        question = _noya_question_from_text(body, bot_username) or body
        if not question:
            return
        await _answer_noya_chat(message, question, use_quota=False)
        return

    if not is_group_chat(message):
        return

    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    if await process_moderation(message, bot, group):
        return

    is_noya = False
    question = _noya_question_from_text(body, bot_username) or ""
    if question:
        is_noya = True
    elif (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    ):
        is_noya = True
        question = body

    replied_body = ""
    replied_has_media = False
    if message.reply_to_message:
        replied_body = extract_message_body(message.reply_to_message)
        replied = message.reply_to_message
        replied_has_media = bool(
            replied.photo
            or replied.sticker
            or getattr(replied, "rich_message", None)
            or (
                replied.document
                and (replied.document.mime_type or "").startswith("image/")
            )
        )
    if is_noya and not question and (replied_body or replied_has_media):
        question = "به این پیام توجه کن و پاسخ بده."
    if is_noya and question:
        if not group.collaboration_enabled:
            return
        await _answer_noya_chat(message, question, use_quota=True)
        return

    if not allow_commands:
        # Edited messages: only Noya chat, not moderation slash-commands.
        if not group.collaboration_enabled or not is_bot_mentioned(message, bot_username):
            return
        question = extract_question_from_mention(message, bot_username)
        if not question and replied_body:
            question = "به این پیام توجه کن و پاسخ بده."
        if question:
            await _answer_noya_chat(message, question, use_quota=True)
        return

    command_name, args = plain_command(body)
    moderation_command = PLAIN_MODERATION_COMMANDS.get(command_name)
    if moderation_command:
        handlers = {
            "warn": lambda: perform_warn(message, bot, args),
            "mute": lambda: perform_scheduled_action(message, bot, "mute", args),
            "unmute": lambda: perform_scheduled_action(message, bot, "unmute", args),
            "ban": lambda: perform_scheduled_action(message, bot, "ban", args),
            "unban": lambda: perform_scheduled_action(message, bot, "unban", args),
            "lock": lambda: perform_scheduled_action(message, bot, "lock", args),
            "unlock": lambda: perform_scheduled_action(message, bot, "unlock", args),
            "delete": lambda: perform_delete(message, bot, args),
            "warns": lambda: perform_show_warnings(message, bot),
        }
        await handlers[moderation_command]()
        return

    if not group.collaboration_enabled or not is_bot_mentioned(message, bot_username):
        return
    question = extract_question_from_mention(message, bot_username)
    if not question and replied_body:
        question = "به این پیام توجه کن و پاسخ بده."
    if not question:
        await message.reply("سوال خود را بنویسید.")
        return
    await _answer_noya_chat(message, question, use_quota=True)


async def _answer_settled_edit(message: Message) -> None:
    """Single entry used by the edit coordinator after text has settled."""
    await _handle_noya_text_message(message, message.bot, allow_commands=False)


def _ensure_edit_coordinator_callback() -> None:
    if noya_edit_coordinator._answer_cb is None:
        noya_edit_coordinator.set_answer_callback(_answer_settled_edit)


@router.message(F.text & ~F.command)
@router.message(F.rich_message)
async def handle_text_message(message: Message, bot: Bot):
    """Plain text and Rich Messages (Mira etc. use RICH_MESSAGE with empty text)."""
    # Empty rich shells must fall through to EmptyBotReplyToUsFilter / edit coordinator.
    if not _message_body(message):
        raise SkipHandler()
    await _handle_noya_text_message(message, bot, allow_commands=True)


class NoyaEditedMessageFilter(BaseFilter):
    """Only track edits that are relevant to Noya (reply / @mention / name / bot)."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        me = await bot.me()
        user = message.from_user
        if user and getattr(user, "is_bot", False) and int(user.id) == int(me.id):
            return False
        replied = message.reply_to_message
        reply_to_us = bool(
            replied and replied.from_user and int(replied.from_user.id) == int(me.id)
        )
        if reply_to_us:
            return True
        if user and getattr(user, "is_bot", False):
            # Other bots editing any message that addresses Noya.
            return is_addressing_noya(
                message,
                bot_id=int(me.id),
                bot_username=me.username or "",
            )
        # Humans: only if they address Noya in the edited body (rare, but useful).
        return is_addressing_noya(
            message,
            bot_id=int(me.id),
            bot_username=me.username or "",
        )


@router.edited_message(NoyaEditedMessageFilter())
async def handle_edited_noya_message(message: Message, bot: Bot):
    """Professional streaming/edit intake — debounce via NoyaEditCoordinator."""
    _ensure_edit_coordinator_callback()
    result = await noya_edit_coordinator.observe_edit(
        message,
        reason="edited_message",
    )
    logger.warning(
        "noya_edited_handler chat=%s msg=%s result=%s flags=%s len=%s",
        message.chat.id,
        message.message_id,
        result,
        _message_kind_flags(message),
        len(_message_body(message)),
    )


class EmptyBotReplyToUsFilter(BaseFilter):
    """Match empty group replies from other bots directed at Noya."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not is_group_chat(message) or not message.from_user:
            return False
        if not getattr(message.from_user, "is_bot", False):
            return False
        if _message_body(message):
            return False
        replied = message.reply_to_message
        if not replied or not replied.from_user:
            return False
        me = await bot.me()
        if int(message.from_user.id) == int(me.id):
            return False
        return int(replied.from_user.id) == int(me.id)


@router.message(EmptyBotReplyToUsFilter())
async def handle_empty_bot_reply_to_noya(message: Message, bot: Bot):
    """Open an edit session for empty bot→Noya shells; coordinator answers once settled."""
    _ensure_edit_coordinator_callback()
    logger.warning(
        "noya_empty_bot_reply_session chat=%s msg=%s from=%s flags=%s",
        message.chat.id,
        message.message_id,
        getattr(message.from_user, "username", None),
        _message_kind_flags(message),
    )
    await noya_edit_coordinator.observe_empty_shell(message, reason="empty_bot_reply")


def _is_noya_image_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    return mime.startswith("image/")


@router.message(F.photo | F.sticker | F.document)
async def handle_media_noya_message(message: Message, bot: Bot):
    """Private/group photo & sticker input for Noya (stickers normalized to JPEG)."""
    if message.sticker is None and message.photo is None and not _is_noya_image_document(message):
        return

    bot_info = await bot.me()
    bot_username = bot_info.username or ""
    caption = (message.caption or "").strip()
    default_ask = "این تصویر / استیکر را ببین و پاسخ بده."

    if is_private_chat(message):
        question = _noya_question_from_text(caption, bot_username) or caption or default_ask
        await _answer_noya_chat(message, question, use_quota=False)
        return

    if not is_group_chat(message):
        return

    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    if await process_moderation(message, bot, group):
        return
    if not group.collaboration_enabled:
        return

    is_noya = False
    question = _noya_question_from_text(caption, bot_username) or ""
    if question:
        is_noya = True
    elif (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    ):
        is_noya = True
        question = caption or default_ask
    elif is_bot_mentioned(message, bot_username):
        is_noya = True
        question = extract_question_from_mention(message, bot_username) or default_ask

    if not is_noya:
        return
    if not question:
        question = default_ask
    await _answer_noya_chat(message, question, use_quota=True)


@router.chat_member()
async def handle_member_update(event: ChatMemberUpdated, bot: Bot, event_update):
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        return
    await process_destination_member_update(
        chat_id=event.chat.id,
        user=event.new_chat_member.user,
        status=event.new_chat_member.status,
        restricted_is_member=bool(getattr(event.new_chat_member, "is_member", False)),
        update_id=event_update.update_id,
        invite_url=event.invite_link.invite_link if event.invite_link else "",
    )
    if event.chat.type == ChatType.CHANNEL:
        return

    group = await get_or_create_group_settings(event.chat.id, event.chat.title or "")
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    joined = old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED} and new_status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }
    left = old_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED} and new_status in {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    }
    if joined and group.welcome_enabled:
        text = render_member_template(
            group.welcome_message,
            event.new_chat_member.user,
            event.chat.title or "گروه",
        )
        await bot.send_message(event.chat.id, text, parse_mode="HTML")
    elif left and group.goodbye_enabled:
        text = render_member_template(
            group.goodbye_message,
            event.old_chat_member.user,
            event.chat.title or "گروه",
        )
        await bot.send_message(event.chat.id, text, parse_mode="HTML")


@router.edited_message()
@router.message()
async def check_message(message: Message, bot: Bot):
    # Empty bot→Noya replies are handled by handle_empty_bot_reply_to_noya (registered
    # earlier). This catch-all only runs moderation for remaining group traffic.
    if not is_group_chat(message) or not message.from_user:
        return
    # Avoid double-handling empty bot replies that are waiting on edits.
    if (
        getattr(message.from_user, "is_bot", False)
        and not _message_body(message)
        and message.reply_to_message
        and message.reply_to_message.from_user
    ):
        try:
            me = await bot.me()
            if int(message.reply_to_message.from_user.id) == int(me.id):
                return
        except Exception:
            pass
    group = await get_or_create_group_settings(message.chat.id, message.chat.title or "")
    if await process_moderation(message, bot, group):
        return



async def safe_delete(message: Message):
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning(
            "Could not delete message %s in chat %s",
            message.message_id,
            message.chat.id,
        )


async def notify_deleted(message: Message, text: str, bot: Bot, reply_markup=None):
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _message_body(message: Message) -> str:
    return extract_message_body(message)


def _message_kind_flags(message: Message) -> str:
    """Compact non-empty content flags for diagnosing empty-text bot replies."""
    flags = []
    for attr in (
        "text",
        "caption",
        "rich_message",
        "photo",
        "sticker",
        "animation",
        "document",
        "voice",
        "video",
        "video_note",
        "audio",
        "poll",
        "dice",
        "checklist",
        "story",
        "venue",
        "contact",
        "location",
        "game",
        "invoice",
        "paid_media",
        "entities",
        "caption_entities",
    ):
        val = getattr(message, attr, None)
        if val:
            flags.append(attr)
    ctype = getattr(message, "content_type", None)
    if ctype:
        flags.append(f"content_type={ctype}")
    body = extract_message_body(message)
    if body and "rich_message" in flags and not (getattr(message, "text", None) or getattr(message, "caption", None)):
        flags.append(f"rich_plain_len={len(body)}")
    return ",".join(flags) or "none"


class BotPeerObserveMiddleware(BaseMiddleware):
    """Log other-bot messages / replies-to-us so missing Telegram delivery is obvious."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat and event.chat.type in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }:
            user = event.from_user
            replied = event.reply_to_message
            replied_user = replied.from_user if replied else None
            try:
                me = await data["bot"].me()
                reply_to_us = bool(replied_user and int(replied_user.id) == int(me.id))
            except Exception:
                reply_to_us = False
            update = data.get("event_update")
            is_edit = bool(getattr(update, "edited_message", None))
            if (user and getattr(user, "is_bot", False)) or reply_to_us:
                # WARNING: production root log level is WARNING (info is silent).
                logger.warning(
                    "noya_peer_update chat=%s msg=%s edit=%s from_id=%s from_bot=%s "
                    "username=%s reply_to_us=%s flags=%s text=%r",
                    event.chat.id,
                    event.message_id,
                    is_edit,
                    getattr(user, "id", None),
                    bool(user and getattr(user, "is_bot", False)),
                    getattr(user, "username", None),
                    reply_to_us,
                    _message_kind_flags(event),
                    (_message_body(event)[:160]),
                )
        return await handler(event, data)


class MultiBotGroupDeduplicationMiddleware(BaseMiddleware):
    """Discard duplicate group updates only after Aiogram matched a handler."""

    def __init__(
        self,
        bot_usernames: tuple[str, ...] = (),
        *,
        ttl_seconds: int = 600,
        max_entries: int = 100_000,
    ):
        self.bot_usernames = frozenset(username.casefold() for username in bot_usernames if username)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen = OrderedDict()

    async def _key(self, event, data):
        chat = getattr(event, "chat", None)
        if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return None
        if isinstance(event, Message):
            me = await data["bot"].me()
            replied = getattr(event, "reply_to_message", None)
            replied_user = getattr(replied, "from_user", None) if replied else None
            # Never drop a direct reply to us — required for bot↔bot roast chains.
            if replied_user is not None and int(replied_user.id) == int(me.id):
                update = data.get("event_update")
                kind = "edited_message" if getattr(update, "edited_message", None) else "message"
                return kind, chat.id, event.message_id
            text = event.text or ""
            target = re.search(r"@([A-Za-z0-9_]{5,})\b", text)
            if target and target.group(1).casefold() in self.bot_usernames:
                username = me.username or ""
                if target.group(1).casefold() != username.casefold():
                    return "not-targeted"
            update = data.get("event_update")
            kind = "edited_message" if getattr(update, "edited_message", None) else "message"
            return kind, chat.id, event.message_id
        if isinstance(event, ChatMemberUpdated):
            return (
                "chat_member",
                chat.id,
                event.new_chat_member.user.id,
                int(event.date.timestamp()),
                str(event.old_chat_member.status),
                str(event.new_chat_member.status),
            )
        return None

    async def __call__(self, handler, event, data):
        key = await self._key(event, data)
        if key == "not-targeted":
            return None
        if key is None:
            return await handler(event, data)
        now = monotonic()
        while self._seen and (
            next(iter(self._seen.values())) <= now - self.ttl_seconds
            or len(self._seen) >= self.max_entries
        ):
            self._seen.popitem(last=False)
        if key in self._seen:
            return None
        self._seen[key] = now
        return await handler(event, data)


def build_dispatcher(bot_username: str = "", *, bot_usernames: tuple[str, ...] = ()) -> Dispatcher:
    dispatcher = Dispatcher()
    # Observe first so we log even if a later middleware short-circuits.
    observe = BotPeerObserveMiddleware()
    dispatcher.message.outer_middleware(observe)
    dispatcher.edited_message.outer_middleware(observe)
    deduplication = MultiBotGroupDeduplicationMiddleware(bot_usernames)
    dispatcher.message.outer_middleware(deduplication)
    dispatcher.edited_message.outer_middleware(deduplication)
    dispatcher.chat_member.outer_middleware(deduplication)
    dispatcher.message.outer_middleware(ForcedMembershipMiddleware())
    dispatcher.edited_message.outer_middleware(ForcedMembershipMiddleware())
    dispatcher.message.outer_middleware(BotStartGateMiddleware())
    dispatcher.edited_message.outer_middleware(BotStartGateMiddleware())
    # Opt-in message archival (no-op unless MESSAGE_ARCHIVE_ENABLED). Runs as an
    # outer middleware so it captures group messages regardless of routing.
    dispatcher.message.outer_middleware(ArchiveMiddleware())
    dispatcher.edited_message.outer_middleware(ArchiveMiddleware())
    # Channel posts are a separate event type; count them for channel analytics.
    dispatcher.channel_post.outer_middleware(ArchiveMiddleware())
    dispatcher.edited_channel_post.outer_middleware(ArchiveMiddleware())

    # Routers are module-level singletons.
    # Tests call build_dispatcher() repeatedly in the same process.
    # Must detach properly by cleaning sub_routers and the private _parent_router.
    # Using the public setter raises "Router is already attached".
    # NOTE: agent_router is included BEFORE the main router so /agent, /adminai
    # and the strict "نویا،" trigger are matched before the catch-all handlers.
    # emoji_router must ALSO precede the main router: the main router ends with an
    # unfiltered @router.message() catch-all (check_message) that otherwise
    # consumes /add_emoji and /set_welcome_emoji before emoji_router ever runs.
    # emoji_router only matches those two exact commands, so it cannot shadow any
    # main-router handler. nouya_router stays last (its channel_post/guest_message
    # handlers are separate event types and are unaffected by message ordering).
    for r in (agent_router, forced_membership_router, emoji_router, router, nouya_router):
        if r._parent_router is not None:
            parent = r._parent_router
            if hasattr(parent, "sub_routers") and r in parent.sub_routers:
                parent.sub_routers.remove(r)
            r._parent_router = None

    dispatcher.include_router(agent_router)
    dispatcher.include_router(forced_membership_router)
    dispatcher.include_router(emoji_router)
    dispatcher.include_router(router)
    dispatcher.include_router(nouya_router)
    return dispatcher


class Command(BaseCommand):
    help = "Run Telegram Bot with aiogram 3"

    def handle(self, *args, **options):
        if not BOT_TOKENS:
            raise CommandError("BOT_TOKENS or BOT_TOKEN is not set in the environment.")

        _original_getaddrinfo = socket.getaddrinfo

        def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

        socket.getaddrinfo = _ipv4_only_getaddrinfo

        async def run():
            bots = [Bot(token=token) for token in BOT_TOKENS]
            try:
                identities = await asyncio.gather(*(bot.get_me() for bot in bots))
                configure_super_admins(ADMIN_IDS)
                dispatcher = build_dispatcher(
                    bot_usernames=tuple(identity.username or "" for identity in identities),
                )
                cleanup_tasks = [asyncio.create_task(notice_cleanup_loop(bot)) for bot in bots]
                usernames = ", ".join(f"@{identity.username}" for identity in identities)
                self.stdout.write(f"{len(bots)} Telegram bots are running: {usernames}")
                try:
                    await dispatcher.start_polling(*bots, close_bot_session=False)
                finally:
                    for task in cleanup_tasks:
                        task.cancel()
                    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            finally:
                await asyncio.gather(*(bot.session.close() for bot in bots))

        try:
            asyncio.run(run())
        finally:
            socket.getaddrinfo = _original_getaddrinfo