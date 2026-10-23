"""Regressions for Noya's per-person multi-affect memory and fictional lore."""
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from botapp.affect import AFFECT_KEYS, BASELINES, classify_multi, context, evolve, zero_vector
from botapp.memory.affect import addressed_to_noya, fetch_persona_facts, get_affect_context, observe_affect
from botapp.models import NoyaAffectProfile, TelegramUser


def _msg(text, *, user=110, chat=-350, chat_type="supergroup", msg=1, reply_bot=False,
         is_bot=False, forwarded=False):
    replied = SimpleNamespace(from_user=SimpleNamespace(id=3000, is_bot=True)) if reply_bot else None
    return SimpleNamespace(
        text=text, caption=None, message_id=msg, date=datetime.now(dt_timezone.utc),
        chat=SimpleNamespace(id=chat, type=chat_type),
        from_user=SimpleNamespace(id=user, is_bot=is_bot),
        reply_to_message=replied,
        bot=SimpleNamespace(id=3000),
        forward_origin=object() if forwarded else None,
    )


class MultiAffectPureTests(SimpleTestCase):
    def test_all_dimensions_have_distinct_percentage_slots(self):
        self.assertEqual(len(AFFECT_KEYS), 31)
        self.assertEqual(len(AFFECT_KEYS), len(set(AFFECT_KEYS)))
        self.assertTrue(all(0 <= n <= 100 for n in zero_vector().values()))

    def test_love_and_hurt_are_simultaneous_not_vetoed_by_positive_cues(self):
        labels = classify_multi("نویا دوستت دارم ولی از دستت دلخورم ❤️", addressed=True)
        self.assertGreater(labels["love"], 0)
        self.assertGreater(labels["hurt"], 0)
        output = evolve(zero_vector(), labels)
        self.assertGreater(output["love"], BASELINES["love"])
        self.assertGreater(output["hurt"], BASELINES["hurt"])

    def test_neutral_message_alters_zero_emotion_scores_even_after_time(self):
        original = evolve(zero_vector(), {"hurt": 12., "love": 10.})
        self.assertEqual(evolve(original, {}, hours_since_previous=720), original)

    def test_independent_scores_are_not_a_probability_distribution(self):
        vector = evolve(zero_vector(), {"love": 20., "trust": 20., "hurt": 20.})
        self.assertTrue(all(0 <= n <= 100 for n in vector.values()))
        self.assertGreater(sum(vector.values()), 100.)
        self.assertIn("independent_scores_0_100", context(vector))

    def test_diminishing_returns_and_time_decay(self):
        first = evolve(zero_vector(), {"hurt": 15.})
        second = evolve(first, {"hurt": 15.})
        self.assertLess(second["hurt"] - first["hurt"], first["hurt"])
        later = evolve(second, {"gratitude": 7.}, hours_since_previous=24*14)
        self.assertLess(later["hurt"], second["hurt"])

    def test_unaddressed_hypothetical_negated_and_sexual_without_adult_gate(self):
        self.assertEqual(classify_multi("دوستت دارم", addressed=False), {})
        self.assertEqual(classify_multi("فرض کن دوستت دارم", addressed=True), {})
        self.assertNotIn("love", classify_multi("دوستت ندارم", addressed=True))
        self.assertNotIn("attraction", classify_multi("برام جذابی", addressed=True))
        self.assertIn("attraction", classify_multi("برام جذابی", addressed=True,
                                                    adult_confirmed=True))


class GlobalAffectPersistenceTests(TestCase):
    def test_one_identity_across_group_and_bot_private_chat(self):
        observe_affect(_msg("نویا دوستت دارم", chat=-350, msg=1))
        observe_affect(_msg("از دستت دلخورم", chat=110, chat_type="private", msg=2))
        user = TelegramUser.objects.get(telegram_user_id=110)
        profile = NoyaAffectProfile.objects.get(user=user)
        self.assertGreater(profile.scores["love"], 0)
        self.assertGreater(profile.scores["hurt"], 0)
        self.assertEqual(len(profile.processed_message_keys), 2)

    def test_neutral_on_new_user_does_not_allocate_emotion_record(self):
        self.assertIsNone(observe_affect(_msg("نویا، قیمت امروز چنده؟", user=333)))
        self.assertFalse(NoyaAffectProfile.objects.filter(
            user__telegram_user_id=333
        ).exists())

    def test_duplicate_and_neutral_do_not_update_scores_or_timestamp(self):
        event = _msg("نویا مرسی", msg=1)
        observe_affect(event)
        profile = NoyaAffectProfile.objects.get(user__telegram_user_id=110)
        scores = dict(profile.scores)
        at = profile.last_affective_at
        observe_affect(event)
        observe_affect(_msg("نویا، هوا امروز چطوره؟", msg=2))
        profile.refresh_from_db()
        self.assertEqual(profile.scores, scores)
        self.assertEqual(profile.last_affective_at, at)
        self.assertEqual(len(profile.processed_message_keys), 1)

    def test_lookalike_username_not_treated_as_noya_mention(self):
        self.assertFalse(addressed_to_noya(_msg("@noya_impersonator دوستت دارم")))
        self.assertTrue(addressed_to_noya(_msg("@nuyarobot ممنون")))

    def test_mentioned_in_group_and_reply_to_bot_both_count(self):
        self.assertTrue(addressed_to_noya(_msg("نویا مرسی")))
        self.assertTrue(addressed_to_noya(_msg("دمت گرم", reply_bot=True)))
        self.assertFalse(addressed_to_noya(_msg("مرسی", reply_bot=False)))
        observe_affect(_msg("دمت گرم", reply_bot=True, msg=1))
        self.assertEqual(NoyaAffectProfile.objects.count(), 1)

    def test_unrelated_private_replies_and_forwarded_text_do_not_forge_affect(self):
        self.assertIsNone(observe_affect(_msg("دوستت دارم", chat=-350, msg=1)))
        self.assertIsNone(observe_affect(_msg("نویا دوستت دارم", forwarded=True, msg=2)))
        self.assertIsNone(observe_affect(_msg("نویا دوستت دارم", is_bot=True, msg=3)))
        self.assertEqual(NoyaAffectProfile.objects.count(), 0)

    def test_profiles_isolated_by_verified_user_id(self):
        observe_affect(_msg("نویا دوستت دارم", user=111))
        self.assertEqual(get_affect_context(112)["independent_scores_0_100"], {})
        self.assertTrue(get_affect_context(111)["independent_scores_0_100"])

    def test_persona_lore_retrieved_only_on_trigger_and_stays_fictional(self):
        self.assertEqual(fetch_persona_facts("پیام معمولی"), [])
        facts = fetch_persona_facts("چند سالته؟")
        self.assertTrue(facts)
        self.assertTrue(all(item["fictional"] for item in facts))
        self.assertLessEqual(len(facts), 3)
        self.assertIn("۲۵", str(facts))
