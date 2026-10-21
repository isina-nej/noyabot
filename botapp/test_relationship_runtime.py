import json
import re
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from botapp.memory.integration import run_ai_with_memory
from botapp.memory.relationship import get_relationship_state
from botapp.models import MemoryConversation, NoyaRelationshipState


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


def _relationship_block(question: str) -> dict:
    match = re.search(
        r"\[RELATIONSHIP_CONTEXT\]\s*(\{.*?\})\s*\[/RELATIONSHIP_CONTEXT\]",
        question,
        re.DOTALL,
    )
    assert match, "relationship context was not injected"
    return json.loads(match.group(1))


@override_settings(MEMORY_RETRIEVAL_ENABLED=False, MEMORY_INGESTION_ENABLED=False)
class RelationshipRuntimeTests(TestCase):
    def test_state_survives_database_reload_with_scoped_values(self):
        async def provider(question, session_id, **kwargs):
            return "پاسخ"

        async_to_sync(run_ai_with_memory)(
            _message(user_id=42, chat_id=-703, message_id=1, text="بی‌احترامی کردی"),
            "سؤال", provider, "telegram:-703",
        )
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-703:0")
        row = NoyaRelationshipState.objects.get(user__telegram_user_id=42, conversation=conversation)
        self.assertEqual(row.hurt_score, 10)
        self.assertEqual(row.relationship_status, "hurt")
        row.refresh_from_db()
        self.assertEqual(get_relationship_state(42, conversation).hurt_score, 10)

    def test_relationship_context_is_injected_and_repair_is_deduplicated(self):
        captured = []

        async def provider(question, session_id, **kwargs):
            captured.append(question)
            return "پاسخ"

        async_to_sync(run_ai_with_memory)(_message(message_id=1, text="بی‌احترامی کردی"), "سؤال", provider, "telegram:-700")
        async_to_sync(run_ai_with_memory)(_message(message_id=2, text="ببخشید، اشتباه کردم"), "سؤال", provider, "telegram:-700")
        async_to_sync(run_ai_with_memory)(_message(message_id=3, text="ببخشید، اشتباه کردم"), "سؤال", provider, "telegram:-700")

        conversation = MemoryConversation.objects.get(conversation_id="telegram:-700:0")
        state = get_relationship_state(42, conversation)
        self.assertEqual(state.hurt_score, 10)
        self.assertEqual(state.repair_progress, 10)
        self.assertEqual(state.reconciliation_count, 1)
        self.assertTrue(all("RELATIONSHIP_CONTEXT" in item for item in captured))

        blocks = [_relationship_block(item) for item in captured]
        self.assertEqual(blocks[0]["relationship_status"], "hurt")
        self.assertIs(blocks[0]["may_request_apology"], False)  # 10 <= mild threshold
        self.assertEqual(blocks[1]["relationship_status"], "reconciled")
        self.assertEqual(blocks[2]["relationship_status"], "reconciled")
        self.assertIs(blocks[2]["may_request_apology"], False)

    def test_three_apologies_yield_one_forgiveness_event(self):
        states = []

        async def provider(question, session_id, **kwargs):
            states.append(_relationship_block(question))
            return "پاسخ"

        async_to_sync(run_ai_with_memory)(
            _message(chat_id=-704, message_id=1, text="خفه شو"), "سؤال", provider, "telegram:-704",
        )
        for message_id, text in ((2, "ببخشید"), (3, "ببخشید ببخشید"), (4, "شرمنده، تقصیر من بود")):
            async_to_sync(run_ai_with_memory)(
                _message(chat_id=-704, message_id=message_id, text=text), "سؤال", provider, "telegram:-704",
            )
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-704:0")
        row = NoyaRelationshipState.objects.get(user__telegram_user_id=42, conversation=conversation)
        self.assertEqual(row.reconciliation_count, 0)
        self.assertEqual(row.relationship_status, "repairing")
        self.assertEqual(row.repair_credits, [f"{row.active_conflict_id}|apology"])
        # Still an active conflict: the model is never told it was forgiven.
        self.assertIs(states[-1]["conflict_active"], True)
        self.assertIn("already_forgave", states[-1]["do_not_claim"])
        self.assertNotIn("requires_new_apology", states[-1]["do_not_claim"])

    def test_model_quality_criticism_does_not_start_a_conflict(self):
        captured = []

        async def provider(question, session_id, **kwargs):
            captured.append(_relationship_block(question))
            return "پاسخ"

        async_to_sync(run_ai_with_memory)(
            _message(chat_id=-705, message_id=1, text="نویا جدی میگم، بعضی وقتا واقعاً جوابای مسخره میدی."),
            "سؤال", provider, "telegram:-705",
        )
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-705:0")
        state = get_relationship_state(42, conversation)
        self.assertEqual(state.hurt_score, 0)
        self.assertEqual(state.relationship_status, "neutral")
        self.assertIs(captured[0]["conflict_active"], False)
        self.assertIn("still_upset", captured[0]["do_not_claim"])

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

    def test_same_apology_can_repair_a_new_conflict_after_forgiveness(self):
        async def provider(question, session_id, **kwargs):
            return "پاسخ"

        for message_id, text in (
            (1, "بی‌احترامی کردی"),
            (2, "ببخشید، اشتباه کردم"),
            (3, "بی‌احترامی کردی"),
            (4, "ببخشید، اشتباه کردم"),
        ):
            async_to_sync(run_ai_with_memory)(
                _message(chat_id=-702, message_id=message_id, text=text),
                "سؤال", provider, "telegram:-702",
            )
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-702:0")
        state = get_relationship_state(42, conversation)
        self.assertEqual(state.remaining_hurt, 0)
        self.assertEqual(state.reconciliation_count, 2)
