from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from botapp.memory.integration import run_ai_with_memory
from botapp.memory.relationship import get_relationship_state
from botapp.models import MemoryConversation


def _message(*, user_id=42, chat_id=-700, message_id=1, text="سلام"):
    return SimpleNamespace(
        message_id=message_id,
        date=datetime(2026, 7, 28, 12, 0, tzinfo=dt_timezone.utc),
        text=text,
        caption=None,
        from_user=SimpleNamespace(id=user_id, is_bot=False, first_name="Ali"),
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="Group", username=None, full_name=None),
        message_thread_id=None,
        forward_origin=None,
    )


@override_settings(MEMORY_RETRIEVAL_ENABLED=False, MEMORY_INGESTION_ENABLED=False)
class RelationshipRuntimeTests(TestCase):
    def test_relationship_context_is_injected_and_repair_is_deduplicated(self):
        captured = []

        async def provider(question, session_id, **kwargs):
            captured.append(question)
            return "پاسخ"

        hurt_message = _message(message_id=1, text="بی‌احترامی کردی")
        async_to_sync(run_ai_with_memory)(hurt_message, "سؤال", provider, "telegram:-700")
        apology = _message(message_id=2, text="ببخشید، اشتباه کردم")
        async_to_sync(run_ai_with_memory)(apology, "سؤال", provider, "telegram:-700")
        duplicate_apology = _message(message_id=3, text="ببخشید، اشتباه کردم")
        async_to_sync(run_ai_with_memory)(duplicate_apology, "سؤال", provider, "telegram:-700")

        conversation = MemoryConversation.objects.get(conversation_id="telegram:-700:0")
        state = get_relationship_state(42, conversation)
        self.assertEqual(state.hurt_score, 10)
        self.assertEqual(state.repair_progress, 10)
        self.assertTrue(any("RELATIONSHIP_CONTEXT" in item for item in captured))

    def test_relationship_context_isolated_by_user_and_chat(self):
        async def provider(question, session_id, **kwargs):
            return "پاسخ"

        async_to_sync(run_ai_with_memory)(
            _message(user_id=42, chat_id=-701, text="بی‌احترامی کردی"),
            "سؤال", provider, "telegram:-701",
        )
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-701:0")
        self.assertEqual(get_relationship_state(99, conversation).hurt_score, 0)
        other_chat = MemoryConversation.objects.create(
            platform="telegram", chat_id=-700, thread_id=0,
            conversation_id="telegram:-700:0", chat_type="supergroup",
        )
        self.assertEqual(get_relationship_state(42, other_chat).hurt_score, 0)
