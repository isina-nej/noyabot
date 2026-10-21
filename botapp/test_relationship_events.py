from django.test import SimpleTestCase

from botapp.memory.relationship_events import classify_social_event


class RelationshipEventDetectionTests(SimpleTestCase):
    def test_apology_and_acknowledgement_are_distinct(self):
        self.assertEqual(classify_social_event("ببخشید، اشتباه کردم").kind, "sincere_apology")
        self.assertEqual(classify_social_event("حق داشتی ناراحت بشی").kind, "acknowledgement_of_harm")

    def test_teasing_does_not_create_hurt(self):
        event = classify_social_event("هار هار، شوخی کردم")
        self.assertEqual(event.kind, "playful_teasing")

    def test_repeated_disrespect_is_more_severe(self):
        event = classify_social_event("باز هم بی‌احترامی کردی")
        self.assertEqual(event.kind, "repeated_disrespect")
        self.assertEqual(event.severity, 45)

    def test_ambiguous_ordinary_message_is_ignored(self):
        self.assertIsNone(classify_social_event("امروز هوا چطوره؟"))
        self.assertIsNone(classify_social_event("از این به بعد تکرار نمی‌کنم"))
