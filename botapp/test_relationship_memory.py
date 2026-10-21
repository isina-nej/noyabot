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
from botapp.models import MemoryConversation, NoyaRelationshipEvent, TelegramUser


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

    def test_context_is_bounded_and_deletion_removes_state_and_events(self):
        state = apply_relationship_event(
            self.user.telegram_user_id, self.chat,
            EmotionalEvent("mild_disrespect", event_id="h", severity=12),
        )
        context = relationship_context(state)
        self.assertEqual(context["remaining_hurt"], 12)
        self.assertNotIn("h", context)
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
