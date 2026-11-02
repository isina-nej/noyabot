from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from botapp.ambient import reset_ambient_state_for_tests, should_join_group_conversation
from botapp.noya_context import recent_chat_lines, remember_group_message


def _message(chat_id=-100, text="امروز هوا واقعاً قشنگه", sender_is_bot=False, chat_type="supergroup"):
    return SimpleNamespace(
        message_id=1,
        text=text, caption=None, photo=None, sticker=None,
        document=None, animation=None, voice=None, audio=None,
        video=None, video_note=None, reply_to_message=None,
        from_user=SimpleNamespace(id=42, is_bot=sender_is_bot, first_name="Tester", full_name="Tester"),
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Test"),
    )


class AmbientReplyTest(SimpleTestCase):
    def setUp(self):
        reset_ambient_state_for_tests()

    def tearDown(self):
        reset_ambient_state_for_tests()

    def test_disabled_by_default(self):
        with patch.dict("os.environ", {"NOYA_AMBIENT_REPLIES_ENABLED": "false"}):
            self.assertFalse(should_join_group_conversation(_message(), now=1000))

    def test_wait_for_multiple_messages_and_cooldown(self):
        cfg = {
            "NOYA_AMBIENT_REPLIES_ENABLED": "true",
            "NOYA_AMBIENT_MIN_MESSAGES": "6",
            "NOYA_AMBIENT_COOLDOWN_SECONDS": "600",
        }
        with patch.dict("os.environ", cfg):
            for _ in range(5):
                self.assertFalse(should_join_group_conversation(_message(), now=1000))
            self.assertTrue(should_join_group_conversation(_message(), now=1000))
            for _ in range(8):
                self.assertFalse(should_join_group_conversation(_message(), now=1002))
            self.assertTrue(should_join_group_conversation(_message(), now=1700))

    def test_do_not_trigger_on_bots_commands_replies_or_channels(self):
        with patch.dict("os.environ", {"NOYA_AMBIENT_REPLIES_ENABLED": "true"}):
            self.assertFalse(should_join_group_conversation(_message(sender_is_bot=True)))
            self.assertFalse(should_join_group_conversation(_message(text="/ban user")))
            self.assertFalse(should_join_group_conversation(_message(chat_type="channel")))
            m = _message()
            m.reply_to_message = SimpleNamespace(message_id=9)
            self.assertFalse(should_join_group_conversation(m))

    def test_channel_post_media_label_is_visible_in_ambient_context_only(self):
        m = _message(chat_id=-10042, text=None, chat_type="channel")
        m.animation = SimpleNamespace(file_id="gif")
        remember_group_message(m)
        self.assertTrue(any("گیف" in line for line in recent_chat_lines(-10042)))
