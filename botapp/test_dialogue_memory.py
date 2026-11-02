"""Durable recall and relationship continuity across requests and identities."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from botapp.memory.dialogue import forget_dialogue, get_dialogue_history
from botapp.memory.integration import get_or_create_memory_conversation, run_ai_with_memory
from botapp.models import NoyaDialogueTurn


def _message(text, *, user_id=42, chat_id=-100, thread=0, msg_id=1):
    return SimpleNamespace(
        text=text, caption=None, message_id=msg_id,
        message_thread_id=thread, date=datetime(2026, 11, 2, tzinfo=timezone.utc),
        from_user=SimpleNamespace(id=user_id, is_bot=False, full_name="Tester"),
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="Test group"),
        forward_origin=None,
    )


class DialogueMemoryTest(TestCase):
    @override_settings(NOYA_DIALOGUE_ENABLED=True, MEMORY_INGESTION_ENABLED=False,
                       MEMORY_RETRIEVAL_ENABLED=False, NOYA_DIALOGUE_MAX_TURNS=12)
    def test_persists_across_requests_only_for_same_speaker_topic(self):
        captured = []
        async def provider(question, session_id, **kwargs):
            captured.append(question)
            return "پاسخ اول" if len(captured) == 1 else "پاسخ دوم"

        async_to_sync(run_ai_with_memory)(
            _message("من کتاب داستان دوست دارم", msg_id=1), "اولین سوال", provider, "telegram:-100"
        )
        self.assertEqual(NoyaDialogueTurn.objects.count(), 2)
        async_to_sync(run_ai_with_memory)(
            _message("قبلاً درباره چی حرف زدیم؟", msg_id=2), "موضوع قبلی چیه؟",
            provider, "telegram:-100",
        )
        self.assertIn("من کتاب داستان دوست دارم", captured[1])
        self.assertIn("پاسخ اول", captured[1])
        self.assertEqual(NoyaDialogueTurn.objects.count(), 4)

        async_to_sync(run_ai_with_memory)(
            _message("سلام", user_id=99, msg_id=3), "سلام",
            provider, "telegram:-100",
        )
        self.assertNotIn("کتاب داستان", captured[2])
        async_to_sync(run_ai_with_memory)(
            _message("سلام", thread=2, msg_id=4), "سلام",
            provider, "telegram:-100",
        )
        self.assertNotIn("کتاب داستان", captured[3])
        async_to_sync(run_ai_with_memory)(
            _message("سلام", chat_id=-200, msg_id=5), "سلام",
            provider, "telegram:-200",
        )
        self.assertNotIn("کتاب داستان", captured[4])

    @override_settings(NOYA_DIALOGUE_ENABLED=True, MEMORY_RETRIEVAL_ENABLED=False)
    def test_media_without_caption_keeps_continuity(self):
        async def provider(question, session_id, **kwargs):
            return "در تصویر یک گربه است"
        async_to_sync(run_ai_with_memory)(
            _message(None), "این عکس رو ببین", provider, "telegram:-100",
        )
        self.assertEqual(NoyaDialogueTurn.objects.count(), 2)
        self.assertIn("رسانه", NoyaDialogueTurn.objects.get(role="user").content)

    @override_settings(NOYA_DIALOGUE_ENABLED=True, MEMORY_RETRIEVAL_ENABLED=False,
                       NOYA_DIALOGUE_MAX_TURNS=4)
    def test_bounded_history_keeps_only_recent_turns(self):
        async def provider(question, session_id, **kwargs):
            return "جواب"
        for i in range(1, 6):
            async_to_sync(run_ai_with_memory)(
                _message(f"پیام {i}", msg_id=i), f"پیام {i}", provider, "telegram:-100",
            )
        conversation = async_to_sync(get_or_create_memory_conversation)(_message("پیام", msg_id=7))
        self.assertEqual(NoyaDialogueTurn.objects.count(), 4)
        self.assertNotIn("پیام 1", get_dialogue_history(conversation, 42))
        self.assertIn("پیام 5", get_dialogue_history(conversation, 42))
        self.assertEqual(forget_dialogue(42, conversation_id=conversation.id), 4)
        self.assertEqual(NoyaDialogueTurn.objects.count(), 0)

    @override_settings(NOYA_DIALOGUE_ENABLED=True, MEMORY_RETRIEVAL_ENABLED=False)
    def test_provider_internal_type_error_does_not_trigger_second_request(self):
        provider = AsyncMock(side_effect=TypeError("provider bug"))
        with self.assertRaises(TypeError):
            async_to_sync(run_ai_with_memory)(
                _message("سلام"), "سلام", provider, "telegram:-100",
            )
        self.assertEqual(provider.await_count, 1)
