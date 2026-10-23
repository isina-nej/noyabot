import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from .deletion import forget_memories
from .integration import get_or_create_memory_conversation, list_user_memories
from .relationship import delete_all_relationship_states
from .affect import delete_person_affect

logger = logging.getLogger("botapp.memory")


def _commands_enabled() -> bool:
    return bool(getattr(settings, "MEMORY_USER_COMMANDS_ENABLED", True))


async def memories_command(message):
    if not _commands_enabled():
        await message.reply("دستورات مدیریت حافظه فعال نیستند.")
        return
    if not getattr(message, "from_user", None):
        return
    if not getattr(message, "text", ""):
        return
    values = await list_user_memories(message)
    if not values:
        await message.reply("حافظه مجازی برای نمایش وجود ندارد.")
        return
    if str(message.chat.type) != "private":
        await message.reply(
            f"{len(values)} حافظه در این گفتگو وجود دارد. برای حفظ حریم خصوصی، نمایش متن فقط در پیوی انجام می‌شود."
        )
        return
    await message.reply("حافظه‌های قابل نمایش:\n" + "\n".join(f"• {value}" for value in values))


async def forget_command(message, query: str):
    if not _commands_enabled():
        await message.reply("دستورات مدیریت حافظه فعال نیستند.")
        return
    query = (query or "").strip()
    if not query:
        await message.reply("استفاده: /forget عبارت")
        return
    conversation = await get_or_create_memory_conversation(message)
    conversation_id = conversation.id
    user_id = int(message.from_user.id)
    if str(message.chat.type) == "private":
        count = await sync_to_async(forget_memories, thread_sensitive=True)(
            user_id,
            query,
            conversation_id=conversation_id,
        )
        count += await sync_to_async(forget_memories, thread_sensitive=True)(
            user_id,
            query,
            memory_scope="user",
        )
    else:
        count = await sync_to_async(forget_memories, thread_sensitive=True)(
            user_id,
            query,
            conversation_id=conversation_id,
            memory_scope=("conversation", "user_conversation"),
        )
    await message.reply(f"{count} حافظه حذف شد.")


async def forget_all_command(message, confirmation: str):
    if not _commands_enabled():
        await message.reply("دستورات مدیریت حافظه فعال نیستند.")
        return
    if confirmation.strip() != "confirm":
        await message.reply("برای حذف کامل حافظه خصوصی بنویسید: /forget_all confirm")
        return
    if str(message.chat.type) != "private":
        await message.reply("حذف کامل حافظه فقط در گفت‌وگوی خصوصی مجاز است.")
        return
    count = await sync_to_async(forget_memories, thread_sensitive=True)(
        int(message.from_user.id),
        "",
    )
    relationship_count = await sync_to_async(delete_all_relationship_states, thread_sensitive=True)(
        int(message.from_user.id),
    )
    affect_count = await sync_to_async(delete_person_affect, thread_sensitive=True)(
        int(message.from_user.id),
    )
    await message.reply(
        f"{count} حافظه، {relationship_count} سابقهٔ رابطه و {affect_count} پروفایل احساسی حذف شد."
    )
