"""Cross-request persistence proof for Noya's relationship ledger.

The relationship must live in the database, not in the model conversation and
not in process memory: these tests create a conflict, drop every in-process
reference (module reload), and re-read the state from the database.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

import botapp.emotions as emotions_module
import botapp.memory.integration as integration_module
import botapp.memory.relationship as relationship_module
import botapp.memory.relationship_events as relationship_events_module
from botapp.models import MemoryConversation, NoyaRelationshipState


def _reinitialize_runtime() -> None:
    """Simulate a process/service restart: rebuild every relationship module."""
    importlib.reload(emotions_module)
    importlib.reload(relationship_module)
    importlib.reload(relationship_events_module)
    importlib.reload(integration_module)


def _message(*, user_id=42, chat_id=-710, message_id=1, text="سلام"):
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


async def _provider(question, session_id, **kwargs):
    return "پاسخ"


@override_settings(MEMORY_RETRIEVAL_ENABLED=False, MEMORY_INGESTION_ENABLED=False)
class RelationshipPersistenceIntegrationTests(TestCase):
    def _run(self, message):
        return async_to_sync(integration_module.run_ai_with_memory)(
            message, "سؤال", _provider, f"telegram:{message.chat.id}",
        )

    def _row(self, chat_id, user_id=42):
        conversation = MemoryConversation.objects.get(conversation_id=f"telegram:{chat_id}:0")
        return NoyaRelationshipState.objects.get(
            user__telegram_user_id=user_id, conversation=conversation
        )

    def test_conflict_and_reconciliation_survive_runtime_reinitialization(self):
        # 1-2. Create a conflict and persist it through the real request path.
        captured: list[str] = []

        async def capturing_provider(question, session_id, **kwargs):
            captured.append(question)
            return "پاسخ"

        async_to_sync(integration_module.run_ai_with_memory)(
            _message(chat_id=-711, message_id=1, text="بی‌احترامی کردی"),
            "سؤال", capturing_provider, "telegram:-711",
        )
        row = self._row(-711)
        self.assertEqual(row.relationship_status, "hurt")
        self.assertGreater(row.hurt_score, 0)
        conflict_id = row.active_conflict_id
        self.assertTrue(conflict_id)

        # 3-5. Reinitialize the runtime modules and reload from the database.
        _reinitialize_runtime()
        reloaded = relationship_module.get_relationship_state(
            42, MemoryConversation.objects.get(conversation_id="telegram:-711:0")
        )
        self.assertEqual(reloaded.relationship_status, "hurt")
        self.assertGreater(reloaded.remaining_hurt, 0)
        self.assertEqual(reloaded.active_conflict_id, conflict_id)

        # 6. Reconcile the conflict (renewed runtime).
        relationship_module.apply_relationship_event(
            42, MemoryConversation.objects.get(conversation_id="telegram:-711:0"),
            "sincere_apology", event_id="telegram:-711:2:social:sincere_apology",
        )
        self.assertEqual(
            relationship_module.get_relationship_state(
                42, MemoryConversation.objects.get(conversation_id="telegram:-711:0")
            ).relationship_status,
            "reconciled",
        )

        # 7-8. Reinitialize again and verify reconciliation survived.
        _reinitialize_runtime()
        conversation = MemoryConversation.objects.get(conversation_id="telegram:-711:0")
        persisted = relationship_module.get_relationship_state(42, conversation)
        context = relationship_module.relationship_context(persisted)
        self.assertEqual(context["relationship_status"], "reconciled")
        self.assertIs(context["conflict_active"], False)
        self.assertIs(context["may_request_apology"], False)
        self.assertEqual(persisted.reconciliation_count, 1)
        self.assertIn("requires_new_apology", context["do_not_claim"])

        # The fresh request injects the reconciled state, not a stale one.
        captured.clear()
        async_to_sync(integration_module.run_ai_with_memory)(
            _message(chat_id=-711, message_id=3, text="خوبی؟"),
            "سؤال", capturing_provider, "telegram:-711",
        )
        block = _relationship_block(captured[-1])
        self.assertEqual(block["relationship_status"], "reconciled")
        self.assertIs(block["conflict_active"], False)

    def test_memory_is_isolated_across_users_and_chats(self):
        self._run(_message(user_id=42, chat_id=-712, message_id=1, text="خفه شو"))
        self._run(_message(user_id=77, chat_id=-712, message_id=1, text="سلام خوبی"))
        self._run(_message(user_id=42, chat_id=-713, message_id=1, text="سلام خوبی"))

        _reinitialize_runtime()
        self.assertEqual(self._row(-712, user_id=42).relationship_status, "hurt")
        self.assertEqual(self._row(-712, user_id=77).relationship_status, "neutral")
        self.assertEqual(self._row(-713, user_id=42).relationship_status, "neutral")
        self.assertEqual(NoyaRelationshipState.objects.count(), 3)

    def test_new_conflict_requires_a_real_negative_event_after_forgiveness(self):
        self._run(_message(chat_id=-714, message_id=1, text="خفه شو"))
        self._run(_message(chat_id=-714, message_id=2, text="ببخشید"))
        # A single apology after serious disrespect is not a full forgiveness.
        self.assertEqual(self._row(-714).relationship_status, "repairing")
        self._run(_message(chat_id=-714, message_id=3, text="حق داشتی ناراحت بشی"))
        row = self._row(-714)
        self.assertEqual(row.relationship_status, "reconciled")
        reconciled_at = row.last_reconciliation_at
        reconciled_count = row.reconciliation_count

        # Friendly chatter and criticism of answer quality must not reopen it.
        for message_id, text in (
            (4, "سلام، خوبی؟"),
            (5, "نویا جدی میگم، بعضی وقتا واقعاً جوابای مسخره میدی."),
            (6, "موافق نیستم، به نظرم اشتباهه"),
        ):
            self._run(_message(chat_id=-714, message_id=message_id, text=text))

        _reinitialize_runtime()
        row = self._row(-714)
        self.assertEqual(row.relationship_status, "reconciled")
        self.assertEqual(row.reconciliation_count, reconciled_count)
        self.assertEqual(row.last_reconciliation_at, reconciled_at)

        # A real new negative event does open a fresh conflict with a new id.
        self._run(_message(chat_id=-714, message_id=7, text="باز هم بی‌احترامی کردی"))
        _reinitialize_runtime()
        row = self._row(-714)
        self.assertEqual(row.relationship_status, "hurt")
        self.assertEqual(row.reconciliation_count, reconciled_count)
        self.assertNotEqual(row.active_conflict_id, "")
        self.assertLessEqual(row.hurt_score, 45)
