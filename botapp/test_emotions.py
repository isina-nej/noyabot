from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from botapp.emotions import EmotionalEvent, EmotionalState, apply_event, decay_state


class EmotionalStateEngineTests(SimpleTestCase):
    def test_seventy_hurt_requires_seventy_repair(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        self.assertEqual(state.hurt_score, 70)
        self.assertEqual(state.remaining_hurt, 70)
        state = apply_event(state, "sincere_apology", event_id="apology")
        state = apply_event(state, "acknowledgement_of_harm", event_id="ack")
        self.assertEqual(state.remaining_hurt, 25)
        state = apply_event(state, "behavioral_improvement", event_id="change")
        self.assertEqual(state.remaining_hurt, 0)
        self.assertTrue(state.reconciled)
        self.assertEqual(state.current_social_mood, "reconciled")

    def test_duplicate_apology_does_not_add_repair_twice(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        state = apply_event(state, "sincere_apology", event_id="same-apology")
        duplicate = apply_event(state, "sincere_apology", event_id="same-apology")
        self.assertEqual(duplicate.repair_progress, 25)

    def test_explicit_repair_points_do_not_become_new_hurt(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        state = apply_event(state, EmotionalEvent("sincere_apology", event_id="apology", repair_points=17, severity=99))
        self.assertEqual(state.hurt_score, 70)
        self.assertEqual(state.repair_progress, 17)

    def test_repeated_negative_behavior_after_reconciliation_starts_new_ledger(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h", severity=20))
        state = apply_event(state, "sincere_apology", event_id="a")
        self.assertTrue(state.reconciled)
        state = apply_event(state, EmotionalEvent("mild_disrespect", event_id="h2", severity=12))
        self.assertEqual(state.repair_progress, 0)
        self.assertEqual(state.remaining_hurt, 32)

    def test_decay_is_slower_for_serious_conflict(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h", severity=80, timestamp=at))
        decayed = decay_state(state, now=at + timedelta(days=10))
        self.assertEqual(decayed.hurt_score, 70)
        minor = apply_event(EmotionalState(), EmotionalEvent("mild_disrespect", event_id="m", severity=10, timestamp=at))
        self.assertEqual(decay_state(minor, now=at + timedelta(days=2)).hurt_score, 2)

    def test_scores_are_bounded_and_event_confidence_scales_effect(self):
        state = apply_event(EmotionalState(), EmotionalEvent("repeated_disrespect", event_id="x", severity=200, confidence=0.5))
        self.assertEqual(state.hurt_score, 100)
        self.assertGreaterEqual(state.warmth, 0)
        self.assertLessEqual(state.playfulness, 100)
