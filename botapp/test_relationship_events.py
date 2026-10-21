from django.test import SimpleTestCase

from botapp.memory.relationship_events import classify_social_event


class RelationshipEventDetectionTests(SimpleTestCase):
    def test_apology_and_acknowledgement_are_distinct(self):
        self.assertEqual(classify_social_event("ببخشید، اشتباه کردم").kind, "sincere_apology")
        self.assertEqual(classify_social_event("حق داشتی ناراحت بشی").kind, "acknowledgement_of_harm")
        self.assertEqual(classify_social_event("درستش کردم، دیگه تکرار نمی‌کنم").kind, "behavioral_improvement")

    def test_teasing_does_not_create_hurt(self):
        event = classify_social_event("هار هار، شوخی کردم")
        self.assertEqual(event.kind, "playful_teasing")
        self.assertEqual(classify_social_event("دوستت دارم نویا 😄").kind, "playful_teasing")
        self.assertEqual(classify_social_event("ناز نکن دیگه 😂").kind, "playful_teasing")

    def test_model_quality_criticism_is_not_relationship_damage(self):
        for text in (
            "نویا جدی میگم، بعضی وقتا واقعاً جوابای مسخره میدی.",
            "این جوابت افتضاح بود",
            "خیلی بد جواب می‌دی",
        ):
            event = classify_social_event(text)
            self.assertEqual(event.kind, "quality_criticism", text)
            from botapp.emotions import EVENT_DEFAULT_SEVERITY
            self.assertEqual(EVENT_DEFAULT_SEVERITY[event.kind], 0)

    def test_criticism_phrase_with_playful_marker_stays_playful(self):
        self.assertEqual(
            classify_social_event("شوخی کردم، جوابات مسخره‌ست 😄").kind,
            "playful_teasing",
        )

    def test_normal_disagreement_is_not_disrespect(self):
        self.assertEqual(classify_social_event("موافق نیستم، به نظرم اشتباهه").kind, "normal_disagreement")
        self.assertEqual(classify_social_event("قبول ندارم، درست نیست").kind, "normal_disagreement")

    def test_repeated_disrespect_is_more_severe(self):
        event = classify_social_event("باز هم بی‌احترامی کردی")
        self.assertEqual(event.kind, "repeated_disrespect")
        self.assertEqual(event.severity, 45)

    def test_serious_insult_is_detected(self):
        self.assertEqual(classify_social_event("خفه شو").kind, "serious_disrespect")
        self.assertEqual(classify_social_event("تو خیلی احمقی").kind, "serious_disrespect")

    def test_ambiguous_ordinary_message_is_ignored(self):
        self.assertIsNone(classify_social_event("امروز هوا چطوره؟"))
        self.assertIsNone(classify_social_event("از این به بعد تکرار نمی‌کنم"))
        self.assertIsNone(classify_social_event(""))
        self.assertIsNone(classify_social_event(None))
        self.assertIsNone(classify_social_event("ساعت چنده؟"))

    def test_gratitude_and_positive_news(self):
        self.assertEqual(classify_social_event("مرسی، دمت گرم").kind, "gratitude")
        self.assertEqual(classify_social_event("چه خبر خوبی، قبول شدم").kind, "positive_news")
