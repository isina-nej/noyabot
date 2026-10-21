from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from botapp.emotions import EmotionalEvent
from botapp.memory.relationship import (
    apply_relationship_event,
    delete_all_relationship_states,
    delete_relationship_state,
    get_relationship_state,
    prune_relationship_events,
    relationship_context,
)
from botapp.models import MemoryConversation, NoyaRelationshipEvent, NoyaRelationshipState, TelegramUser

CONTEXT_KEYS = {
    "authoritative",
    "relationship_status",
    "conflict_active",
    "conflict_is_reconciled",
    "may_request_apology",
    "may_reference_past_conflict_playfully",
    "warmth_level",
    "playfulness_level",
    "trust_level",
    "tone_directive",
    "do_not_claim",
}


class RelationshipPersistenceTests(TestCase):
    def setUp(self):
        self.user = TelegramUser.objects.create(telegram_user_id=9001)
        self.other = TelegramUser.objects.create(telegram_user_id=9002)
        self.chat = MemoryConversation.objects.create(
            platform="telegram", chat_id=-9001, thread_id=0,
            conversation_id="telegram:-9001:0", chat_type="supergroup",
        )
        self.other_chat = MemoryConversation.objects.create(
            platform="telegram", chat_id=-9002, thread_id=0,
            conversation_id="telegram:-9002:0", chat_type="supergroup",
        )

    def test_state_is_scoped_by_user_and_conversation(self):
        apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("serious_disrespect", event_id="h", severity=70),
        )
        self.assertEqual(get_relationship_state(self.user.telegram_user_id, self.chat).hurt_score, 70)
        self.assertEqual(get_relationship_state(self.other.telegram_user_id, self.chat).hurt_score, 0)
        self.assertEqual(get_relationship_state(self.user.telegram_user_id, self.other_chat).hurt_score, 0)

    def test_duplicate_event_does_not_add_hurt_twice(self):
        for _ in range(2):
            apply_relationship_event(
                self.user.telegram_user_id, self.chat,
                EmotionalEvent("serious_disrespect", event_id="same", severity=70),
            )
        self.assertEqual(get_relationship_state(self.user.telegram_user_id, self.chat).hurt_score, 70)
        self.assertEqual(NoyaRelationshipEvent.objects.count(), 1)

    def test_context_is_bounded_and_never_exposes_scores(self):
        state = apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("mild_disrespect", event_id="h", severity=12),
        )
        context = relationship_context(state)
        self.assertEqual(set(context), CONTEXT_KEYS)
        self.assertNotIn("h", context)
        for numeric in ("hurt_score", "repair_progress", "remaining_hurt", "warmth", "trust_context"):
            self.assertNotIn(numeric, context)
        self.assertEqual(context["relationship_status"], "hurt")
        self.assertIs(context["conflict_active"], True)
        self.assertIn("already_forgave", context["do_not_claim"])
        self.assertTrue(context["tone_directive"])

    def test_context_follows_every_phase(self):
        neutral = relationship_context(get_relationship_state(self.user.telegram_user_id, self.chat))
        self.assertEqual(neutral["relationship_status"], "neutral")
        self.assertIs(neutral["conflict_active"], False)
        self.assertIs(neutral["may_request_apology"], False)
        self.assertIs(neutral["may_reference_past_conflict_playfully"], False)
        self.assertIn("still_upset", neutral["do_not_claim"])

        hurt = relationship_context(apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("serious_disrespect", event_id="conflict", severity=40),
        ))
        self.assertEqual(hurt["relationship_status"], "hurt")
        self.assertIs(hurt["may_request_apology"], True)

        repairing = relationship_context(apply_relationship_event(
            self.user.telegram_user_id, self.chat, "sincere_apology", event_id="apology",
        ))
        self.assertEqual(repairing["relationship_status"], "repairing")
        self.assertIs(repairing["conflict_active"], True)

        reconciled = relationship_context(apply_relationship_event(
            self.user.telegram_user_id, self.chat, "acknowledgement_of_harm", event_id="ack",
        ))
        self.assertEqual(reconciled["relationship_status"], "reconciled")
        self.assertIs(reconciled["conflict_is_reconciled"], True)
        self.assertIs(reconciled["conflict_active"], False)
        self.assertIs(reconciled["may_request_apology"], False)
        self.assertIs(reconciled["may_reference_past_conflict_playfully"], True)
        self.assertIn("requires_new_apology", reconciled["do_not_claim"])
        self.assertIn("waiting_for_apology", reconciled["do_not_claim"])

    def test_three_apologies_persist_one_forgiveness_event(self):
        apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("serious_disrespect", event_id="conflict", severity=40),
        )
        for index in range(3):
            apply_relationship_event(
                self.user.telegram_user_id, self.chat,
                "sincere_apology", event_id=f"apology-{index}",
            )
        row = NoyaRelationshipState.objects.get(
            user__telegram_user_id=self.user.telegram_user_id, conversation=self.chat,
        )
        self.assertEqual(row.repair_progress, 25)
        self.assertEqual(row.reconciliation_count, 0)
        self.assertEqual(row.relationship_status, "repairing")
        apply_relationship_event(self.user.telegram_user_id, self.chat, "acknowledgement_of_harm", event_id="ack")
        row.refresh_from_db()
        self.assertEqual(row.reconciliation_count, 1)
        self.assertEqual(row.relationship_status, "reconciled")
        self.assertEqual(row.active_conflict_id, "")
        reconciled_at = row.last_reconciliation_at
        # Apologies after forgiveness never re-forgive or re-open the conflict.
        apply_relationship_event(self.user.telegram_user_id, self.chat, "sincere_apology", event_id="late")
        row.refresh_from_db()
        self.assertEqual(row.reconciliation_count, 1)
        self.assertEqual(row.relationship_status, "reconciled")
        self.assertEqual(row.last_reconciliation_at, reconciled_at)

    def test_reconciled_state_is_stable_across_reads_and_decay(self):
        apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("mild_disrespect", event_id="conflict", severity=10),
        )
        apply_relationship_event(self.user.telegram_user_id, self.chat, "sincere_apology", event_id="apology")
        for _ in range(3):
            state = get_relationship_state(self.user.telegram_user_id, self.chat)
            self.assertEqual(state.relationship_status, "reconciled")
            self.assertEqual(state.reconciliation_count, 1)
            self.assertIsNone(state.active_conflict_id)
        future = timezone.now() + timedelta(days=60)
        later = get_relationship_state(self.user.telegram_user_id, self.chat, now=future)
        self.assertEqual(later.relationship_status, "reconciled")
        self.assertEqual(later.reconciliation_count, 1)

    def test_ordinary_messages_never_start_a_conflict(self):
        for kind in ("friendly_message", "quality_criticism", "normal_disagreement", "playful_teasing"):
            state = apply_relationship_event(
                self.user.telegram_user_id, self.chat, kind, event_id=f"kind-{kind}",
            )
            self.assertEqual(state.relationship_status, "neutral", kind)
            self.assertEqual(state.hurt_score, 0, kind)

    def test_deletion_removes_state_and_events(self):
        apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("mild_disrespect", event_id="h", severity=12),
        )
        self.assertEqual(delete_relationship_state(self.user.telegram_user_id, self.chat), 1)
        self.assertFalse(NoyaRelationshipEvent.objects.exists())

    def test_prune_removes_only_old_events(self):
        apply_relationship_event(self.user.telegram_user_id, self.chat, "friendly_message", event_id="new")
        NoyaRelationshipEvent.objects.filter(event_id="new").update(
            created_at=timezone.now() - timedelta(days=120)
        )
        removed = prune_relationship_events(max_age_days=90)
        self.assertEqual(removed, 1)

    def test_delete_all_states_is_scoped_to_user(self):
        apply_relationship_event(self.user.telegram_user_id, self.chat, "mild_disrespect", event_id="u1")
        apply_relationship_event(self.other.telegram_user_id, self.chat, "mild_disrespect", event_id="u2")
        self.assertEqual(delete_all_relationship_states(self.user.telegram_user_id), 1)
        self.assertEqual(get_relationship_state(self.user.telegram_user_id, self.chat).hurt_score, 0)
        self.assertEqual(get_relationship_state(self.other.telegram_user_id, self.chat).hurt_score, 12)

    @override_settings(NOYA_HURT_MILD_MAX=5)
    def test_emotional_bands_are_configurable(self):
        state = apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("mild_disrespect", event_id="config", severity=4),
        )
        self.assertEqual(state.current_social_mood, "mild_sulking")

    @override_settings(NOYA_RELATIONSHIP_EVENT_RETENTION_DAYS=1)
    def test_state_access_prunes_stale_events(self):
        apply_relationship_event(self.user.telegram_user_id, self.chat, "friendly_message", event_id="old")
        NoyaRelationshipEvent.objects.filter(event_id="old").update(
            created_at=timezone.now() - timedelta(days=3)
        )
        get_relationship_state(self.user.telegram_user_id, self.chat)
        self.assertFalse(NoyaRelationshipEvent.objects.filter(event_id="old").exists())
