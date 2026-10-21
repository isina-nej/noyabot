from django.test import SimpleTestCase

from botapp.agent.responses import clean_model_response


class ModelResponseCleaningTests(SimpleTestCase):
    def test_internal_planning_note_is_not_sent(self):
        raw = "خوبم 🙂\n→ skipped: enthusiasm, add when: funny.\nبگو ببینم"
        self.assertEqual(clean_model_response(raw), "خوبم 🙂\nبگو ببینم")

    def test_code_blocks_and_normal_arrows_are_preserved(self):
        raw = "این فلش را نگه دار: →\n```text\n→ skipped: x, add when: y\n```"
        self.assertEqual(clean_model_response(raw), raw)
