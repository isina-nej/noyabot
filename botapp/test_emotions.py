from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from botapp.emotions import (
    EmotionalEvent,
    EmotionalState,
    apply_event,
    decay_state,
    derive_relationship_status,
)


class EmotionalStateEngineTests(SimpleTestCase):
    def test_seventy_hurt_requires_seventy_repair(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        self.assertEqual(state.hurt_score, 70)
        self.assertEqual(state.remaining_hurt, 70)
        self.assertEqual(state.relationship_status, "hurt")
        state = apply_event(state, "sincere_apology", event_id="apology")
        state = apply_event(state, "acknowledgement_of_harm", event_id="ack")
        self.assertEqual(state.remaining_hurt, 25)
        self.assertEqual(state.relationship_status, "repairing")
        state = apply_event(state, "behavioral_improvement", event_id="change")
        self.assertEqual(state.remaining_hurt, 0)
        self.assertTrue(state.reconciled)
        self.assertEqual(state.current_social_mood, "reconciled")
        self.assertEqual(state.relationship_status, "reconciled")
        self.assertEqual(state.reconciliation_count, 1)

    def test_duplicate_apology_does_not_add_repair_twice(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        state = apply_event(state, "sincere_apology", event_id="same-apology")
        duplicate = apply_event(state, "sincere_apology", event_id="same-apology")
        self.assertEqual(duplicate.repair_progress, 25)

    def test_three_identical_apologies_grant_one_credit_and_one_forgiveness(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="conflict", severity=40))
        for index in range(3):
            state = apply_event(state, "sincere_apology", event_id=f"apology-{index}")
        self.assertEqual(state.repair_progress, 25)
        self.assertEqual(state.reconciliation_count, 0)
        self.assertEqual(state.relationship_status, "repairing")
        # A fourth apology after the conflict closes must not forgive again.
        state = apply_event(state, "acknowledgement_of_harm", event_id="ack")
        self.assertEqual(state.relationship_status, "reconciled")
        self.assertEqual(state.reconciliation_count, 1)
        reconciled_at = state.last_reconciliation_at
        later = apply_event(state, "sincere_apology", event_id="late-apology", now=reconciled_at + timedelta(hours=1))
        self.assertEqual(later.reconciliation_count, 1)
        self.assertEqual(later.last_reconciliation_at, reconciled_at)
        self.assertEqual(later.repair_progress, state.repair_progress)

    def test_differently_worded_apologies_share_the_same_conflict_credit(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="conflict", severity=40))
        first = apply_event(state, EmotionalEvent("sincere_apology", event_id="a1"))
        second = apply_event(first, EmotionalEvent("sincere_apology", event_id="a2", repair_points=99))
        self.assertEqual(second.repair_progress, first.repair_progress)
        self.assertEqual(second.reconciliation_count, 0)

    def test_explicit_repair_points_do_not_become_new_hurt(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="hurt", severity=70))
        state = apply_event(state, EmotionalEvent("sincere_apology", event_id="apology", repair_points=17, severity=99))
        self.assertEqual(state.hurt_score, 70)
        self.assertEqual(state.repair_progress, 17)

    def test_repeated_negative_behavior_after_reconciliation_starts_new_ledger(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h", severity=20))
        state = apply_event(state, "sincere_apology", event_id="a")
        self.assertTrue(state.reconciled)
        self.assertEqual(state.reconciliation_count, 1)
        state = apply_event(state, EmotionalEvent("mild_disrespect", event_id="h2", severity=12))
        # Only the residual (zero) hurt carries into the new conflict.
        self.assertEqual(state.repair_progress, 0)
        self.assertEqual(state.remaining_hurt, 12)
        self.assertEqual(state.hurt_score, 12)
        self.assertEqual(state.relationship_status, "hurt")
        self.assertEqual(state.reconciliation_count, 1)
        forgiven = apply_event(state, "sincere_apology", event_id="a2")
        self.assertEqual(forgiven.relationship_status, "reconciled")
        self.assertEqual(forgiven.reconciliation_count, 2)

    def test_new_negative_event_inside_open_conflict_opens_new_incident(self):
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h1", severity=40))
        state = apply_event(state, "sincere_apology", event_id="a1")
        self.assertEqual(state.repair_progress, 25)
        escalated = apply_event(state, EmotionalEvent("mild_disrespect", event_id="h2", severity=12))
        self.assertEqual(escalated.hurt_score, 27)  # 15 residual + 12 new, no inherited credit
        self.assertEqual(escalated.repair_progress, 0)
        self.assertEqual(escalated.relationship_status, "hurt")
        self.assertEqual(escalated.repair_credits, ())

    def test_apology_without_conflict_is_a_no_op(self):
        neutral = apply_event(EmotionalState(), "sincere_apology", event_id="a0")
        self.assertEqual(neutral.relationship_status, "neutral")
        self.assertEqual(neutral.reconciliation_count, 0)
        reconciled = apply_event(
            apply_event(EmotionalState(), EmotionalEvent("mild_disrespect", event_id="h", severity=12)),
            "sincere_apology",
            event_id="a1",
        )
        self.assertEqual(reconciled.relationship_status, "reconciled")
        apology_only = apply_event(reconciled, "sincere_apology", event_id="a2")
        self.assertEqual(apology_only.relationship_status, "reconciled")
        self.assertEqual(apology_only.reconciliation_count, 1)
        self.assertEqual(apology_only.hurt_score, 12)
        self.assertEqual(apology_only.repair_progress, 12)

    def test_non_emotional_events_never_change_the_ledger(self):
        state = apply_event(EmotionalState(), EmotionalEvent("mild_disrespect", event_id="h", severity=12))
        for index, kind in enumerate(("friendly_message", "compliment", "gratitude", "shared_humor", "positive_news")):
            state = apply_event(state, kind, event_id=f"pos-{index}")
        self.assertEqual(state.hurt_score, 12)
        self.assertEqual(state.relationship_status, "hurt")
        criticism = apply_event(state, "quality_criticism", event_id="crit")
        self.assertEqual(criticism.hurt_score, 12)
        self.assertEqual(criticism.relationship_status, "hurt")

    def test_decay_is_slower_for_serious_conflict(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h", severity=80, timestamp=at))
        decayed = decay_state(state, now=at + timedelta(days=10))
        self.assertEqual(decayed.hurt_score, 70)
        minor = apply_event(EmotionalState(), EmotionalEvent("mild_disrespect", event_id="m", severity=10, timestamp=at))
        self.assertEqual(decay_state(minor, now=at + timedelta(days=2)).hurt_score, 2)

    def test_decay_can_close_a_conflict_once_and_never_reactivates_it(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = apply_event(EmotionalState(), EmotionalEvent("mild_disrespect", event_id="h", severity=8, timestamp=at))
        closed = decay_state(state, now=at + timedelta(days=5))
        self.assertEqual(closed.relationship_status, "reconciled")
        self.assertEqual(closed.reconciliation_count, 1)
        self.assertIsNone(closed.active_conflict_id)
        again = decay_state(closed, now=at + timedelta(days=30))
        self.assertEqual(again.reconciliation_count, 1)
        self.assertEqual(again.relationship_status, "reconciled")
        self.assertEqual(again.hurt_score, 0)

    def test_decay_never_raises_hurt(self):
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = apply_event(EmotionalState(), EmotionalEvent("serious_disrespect", event_id="h", severity=50, timestamp=at))
        self.assertLessEqual(decay_state(state, now=at + timedelta(days=3)).hurt_score, state.hurt_score)

    def test_status_invariants_matrix(self):
        cases = [
            # (hurt, repair, credits, count, expected status)
            (0, 0, (), 0, "neutral"),
            (40, 0, (), 0, "hurt"),
            (40, 25, ("c|apology",), 0, "repairing"),
            (40, 40, ("c|apology", "c|acknowledgement"), 1, "reconciled"),
            (0, 0, (), 1, "reconciled"),
        ]
        for hurt, repair, credits, count, expected in cases:
            self.assertEqual(
                derive_relationship_status(
                    hurt_score=hurt,
                    repair_progress=repair,
                    active_conflict_id="c" if hurt else None,
                    repair_credits=credits,
                    reconciliation_count=count,
                ),
                expected,
                (hurt, repair, credits, count),
            )

    def test_scores_are_bounded_and_event_confidence_scales_effect(self):
        state = apply_event(EmotionalState(), EmotionalEvent("repeated_disrespect", event_id="x", severity=200, confidence=0.5))
        self.assertEqual(state.hurt_score, 100)
        self.assertGreaterEqual(state.warmth, 0)
        self.assertLessEqual(state.playfulness, 100)
        self.assertEqual(state.relationship_status, "hurt")
