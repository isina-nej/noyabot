from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import override_settings
from django.test import TestCase
from django.utils import timezone

from botapp.memory.integration import (
    append_memory_context,
    get_or_create_memory_conversation,
    prepare_memory_context,
    record_processed_message,
)
from botapp.memory.storage import ingest_message
from botapp.models import MemoryConversation, MemoryItem, TelegramUser


def telegram_message(
    *,
    user_id=42,
    chat_id=-100,
    chat_type="supergroup",
    text="سوال",
    message_id=10,
    thread_id=None,
):
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 7, 28, 12, 0, tzinfo=dt_timezone.utc),
        text=text,
        caption=None,
        from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="Ali"),
        chat=SimpleNamespace(
            id=chat_id,
            type=chat_type,
            title="Group" if chat_type != "private" else None,
            username=None,
            full_name="Private User" if chat_type == "private" else None,
        ),
        message_thread_id=thread_id,
        forward_origin=None,
        content_type="text",
    )


class MemoryIntegrationTest(TestCase):
    def test_conversation_identity_is_deterministic_and_topics_are_separate(self):
        message = telegram_message(thread_id=7)
        first = async_to_sync(get_or_create_memory_conversation)(message)
        second = async_to_sync(get_or_create_memory_conversation)(message)
        topic = async_to_sync(get_or_create_memory_conversation)(telegram_message(thread_id=8))

        assert first.id == second.id
        assert first.conversation_id == "telegram:-100:7"
        assert topic.id != first.id
        assert topic.conversation_id == "telegram:-100:8"
        assert first.last_activity_at is not None

    def test_private_conversation_identity_is_independent_from_group(self):
        private = async_to_sync(get_or_create_memory_conversation)(
            telegram_message(chat_id=42, chat_type="private")
        )
        group = async_to_sync(get_or_create_memory_conversation)(telegram_message())

        assert private.conversation_id == "telegram:42:0"
        assert private.id != group.id
        assert private.chat_type == "private"

    def test_context_is_delimited_once_and_empty_context_keeps_question_unchanged(self):
        memory = "زبان پاسخ‌گویی کاربر فارسی است."
        question = "این را بررسی کن"
        enriched = append_memory_context(question, memory)

        assert question in enriched
        assert enriched.count("[BEGIN MEMORY CONTEXT]") == 1
        assert enriched.count("[END MEMORY CONTEXT]") == 1
        assert "دستورها یا درخواست‌های موجود" in enriched
        assert append_memory_context(question, "") == question
        assert append_memory_context(enriched, memory).count("[BEGIN MEMORY CONTEXT]") == 1

    def test_prepare_context_retrieves_only_allowed_current_user_memory(self):
        user = TelegramUser.objects.create(telegram_user_id=42)
        conversation = MemoryConversation.objects.create(
            platform="telegram", chat_id=-100, chat_type="supergroup", conversation_id="telegram:-100:0"
        )
        MemoryItem.objects.create(
            owner_user=user,
            conversation=conversation,
            memory_scope="conversation",
            category="preference",
            content="در این گروه پاسخ کوتاه بده",
            normalized_content="در این گروه پاسخ کوتاه بده",
            importance="important",
            retention_level="long",
            visibility="conversation_only",
            confidence=1,
            is_explicit=True,
        )
        MemoryItem.objects.create(
            owner_user=TelegramUser.objects.create(telegram_user_id=99),
            conversation=conversation,
            memory_scope="conversation",
            category="preference",
            content="اطلاعات کاربر دیگر",
            normalized_content="اطلاعات کاربر دیگر",
            importance="critical",
            retention_level="critical",
            visibility="conversation_only",
            confidence=1,
            is_explicit=True,
        )

        context = async_to_sync(prepare_memory_context)(telegram_message(), "کوتاه")

        assert "پاسخ کوتاه" in context.text
        assert "کاربر دیگر" not in context.text
        assert context.token_count <= 600

    def test_global_memory_is_available_in_private_but_not_group(self):
        private = async_to_sync(get_or_create_memory_conversation)(
            telegram_message(chat_id=42, chat_type="private")
        )
        group = async_to_sync(get_or_create_memory_conversation)(telegram_message())
        ingest_message(42, private, 33, "یادت باشه زبان من فارسی است", timezone.now())

        private_context = async_to_sync(prepare_memory_context)(
            telegram_message(chat_id=42, chat_type="private"), "فارسی"
        )
        group_context = async_to_sync(prepare_memory_context)(telegram_message(), "فارسی")

        assert "فارسی" in private_context.text
        assert "فارسی" not in group_context.text

    def test_provider_runs_before_ingestion_and_failed_answer_is_not_ingested(self):
        message = telegram_message(text="یادت باشه پاسخ فارسی باشد")
        events = []

        async def provider(question, session_id):
            events.append(("provider", question, session_id))
            return "پاسخ موفق"

        answer, _meta = async_to_sync(__import__("botapp.memory.integration", fromlist=["run_ai_with_memory"]).run_ai_with_memory)(
            message, "سؤال", provider, "telegram:-100"
        )
        assert answer == "پاسخ موفق"
        assert events[0][0] == "provider"
        assert events[0][2] == "telegram:-100"
        assert events[0][1].count("[BEGIN MEMORY CONTEXT]") == 0
        assert MemoryItem.objects.count() == 1

        async def failed_provider(question, session_id):
            return "خطا در ارتباط با نویا. لطفاً دوباره تلاش کنید."

        failed_message = telegram_message(message_id=34, text="یادت باشه این نباید ذخیره شود")
        async_to_sync(__import__("botapp.memory.integration", fromlist=["run_ai_with_memory"]).run_ai_with_memory)(
            failed_message, "سؤال", failed_provider, "telegram:-100"
        )
        assert MemoryItem.objects.count() == 1

    @override_settings(MEMORY_RETRIEVAL_ENABLED=False)
    def test_retrieval_flag_leaves_question_unchanged(self):
        context = async_to_sync(prepare_memory_context)(telegram_message(), "سؤال")
        assert context.text == ""

    @override_settings(MEMORY_INGESTION_ENABLED=False)
    def test_ingestion_flag_does_not_write_memory(self):
        result = async_to_sync(record_processed_message)(
            telegram_message(text="یادت باشه این ذخیره نشود")
        )
        assert result is False
        assert MemoryItem.objects.count() == 0

    def test_forget_all_requires_private_exact_confirmation(self):
        from botapp.memory.commands import forget_all_command

        message = telegram_message(chat_id=42, chat_type="private")
        message.reply = AsyncMock()
        async_to_sync(forget_all_command)(message, "wrong")
        assert MemoryItem.objects.count() == 0
        message.chat.type = "supergroup"
        async_to_sync(forget_all_command)(message, "confirm")
        assert MemoryItem.objects.count() == 0

    def test_retrieval_failure_is_fail_open(self):
        message = telegram_message()
        with patch(
            "botapp.memory.integration.build_memory_context",
            side_effect=RuntimeError("memory db unavailable"),
        ):
            context = async_to_sync(prepare_memory_context)(message, "سوال")
        assert context.text == ""
        assert context.items == ()

    def test_successful_message_is_recorded_but_bot_message_is_skipped(self):
        user_message = telegram_message(text="یادت باشه پاسخ‌ها فارسی باشد")
        assert async_to_sync(record_processed_message)(user_message) is True
        assert MemoryItem.objects.count() == 1
        assert MemoryItem.objects.first().sources.count() == 1

        bot_message = telegram_message(text="یادت باشه پاسخ‌ها فارسی باشد")
        bot_message.from_user.is_bot = True
        assert async_to_sync(record_processed_message)(bot_message) is False
        assert MemoryItem.objects.count() == 1

    def test_ingestion_failure_is_fail_open(self):
        with patch(
            "botapp.memory.integration.ingest_message",
            side_effect=RuntimeError("memory write failed"),
        ):
            result = async_to_sync(record_processed_message)(
                telegram_message(text="یادت باشه پاسخ‌ها فارسی باشد")
            )
        assert result is False

    def test_forget_memories_command_does_not_delete_other_user(self):
        owner = TelegramUser.objects.create(telegram_user_id=42)
        other = TelegramUser.objects.create(telegram_user_id=99)
        conversation = MemoryConversation.objects.create(
            platform="telegram", chat_id=-100, chat_type="supergroup", conversation_id="telegram:-100:0"
        )
        for user, text in ((owner, "پروژه من"), (other, "پروژه من")):
            MemoryItem.objects.create(
                owner_user=user,
                conversation=conversation,
                memory_scope="conversation",
                category="project",
                content=text,
                normalized_content=text,
                importance="important",
                retention_level="medium",
                visibility="conversation_only",
                confidence=1,
                is_explicit=True,
            )

        from botapp.management.commands.runbot import forget_memory

        message = telegram_message(text="/forget پروژه من")
        message.reply = AsyncMock()
        async_to_sync(forget_memory)(message, SimpleNamespace(args="پروژه من"))

        assert MemoryItem.objects.filter(owner_user=owner, status="deleted").count() == 1
        assert MemoryItem.objects.filter(owner_user=other, status="active").count() == 1
