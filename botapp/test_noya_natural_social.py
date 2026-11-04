"""Regression tests for Noya's natural group greetings."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from botapp.noya_attention import quick_attention_reply


def make_message(text, reply=None):
    m = SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=42, is_bot=False),
        reply_to_message=reply, message_id=101,
        chat=SimpleNamespace(id=-123, type="supergroup", title="group"),
        photo=None, sticker=None, animation=None, video=None,
        voice=None, audio=None, video_note=None, document=None,
        reply=AsyncMock(),
    )
    m.bot = SimpleNamespace(me=AsyncMock(
        return_value=SimpleNamespace(id=999, username="NuyaRobot"),
    ))
    return m


class AttentionTests(SimpleTestCase):
    def test_only_bare_name(self):
        for s in ("نویا", "نویا!", "نُویا", "Noya", "@NuyaRobot"):
            self.assertEqual(quick_attention_reply(make_message(s)),
                             "جانم، بگو ببینم چی شده؟")
        for s in ("نویا سلام", "نویا بتول کیه", "نویا آزمون داشتی"):
            self.assertIsNone(quick_attention_reply(make_message(s)))

    def test_quoted_message_keeps_context(self):
        self.assertIsNone(quick_attention_reply(make_message(
            "نویا", SimpleNamespace(text="از این عکس بگو"),
        )))

    def test_bare_name_skips_remote_model(self):
        from botapp.management.commands.runbot import _answer_noya_chat
        m = make_message("نویا")
        with (
            patch("botapp.management.commands.runbot.consume_group_quota",
                  new_callable=AsyncMock) as quota,
            patch("botapp.management.commands.runbot.run_ai_with_memory",
                  new_callable=AsyncMock) as llm,
        ):
            async_to_sync(_answer_noya_chat)(m, "سلام", use_quota=True)
        m.reply.assert_awaited_once_with("جانم، بگو ببینم چی شده؟")
        quota.assert_not_awaited()
        llm.assert_not_awaited()


class SupersededTimeoutTests(SimpleTestCase):
    def tearDown(self):
        from botapp.noya_turns import reset_for_tests
        reset_for_tests()

    def test_only_newer_same_speaker_group_turn_suppresses_timeout(self):
        from botapp.noya_turns import remember_turn, superseded_timeout
        old = make_message("نویا نظرت چیه")
        old.message_id = 10
        new = make_message("نویا یه سؤال دیگه")
        new.message_id = 15
        remember_turn(old, 999)
        self.assertFalse(superseded_timeout(old, 999))
        remember_turn(new, 999)
        self.assertTrue(superseded_timeout(old, 999))
        self.assertFalse(superseded_timeout(old, 1000))
        self.assertFalse(superseded_timeout(new, 999))
        old.chat.type = "private"
        self.assertFalse(superseded_timeout(old, 999))

    def test_different_people_and_topics_do_not_supersede_each_other(self):
        from botapp.noya_turns import remember_turn, superseded_timeout
        first = make_message("نویا سوال یک")
        first.message_id = 10
        next_person = make_message("نویا سلام")
        next_person.message_id = 20
        next_person.from_user.id = 88
        remember_turn(first, 999)
        remember_turn(next_person, 999)
        self.assertFalse(superseded_timeout(first, 999))
        other_thread = make_message("نویا سلام")
        other_thread.message_id = 30
        other_thread.message_thread_id = 6
        remember_turn(other_thread, 999)
        self.assertFalse(superseded_timeout(first, 999))

    def test_opt_out(self):
        import os
        from botapp.noya_turns import remember_turn, superseded_timeout
        old = make_message("نویا سوال یک")
        old.message_id = 10
        latest = make_message("نویا سوال دو")
        latest.message_id = 20
        remember_turn(old, 999)
        remember_turn(latest, 999)
        with patch.dict(os.environ, {"NOYA_SUPPRESS_SUPERSEDED_TIMEOUTS": "false"}):
            self.assertFalse(superseded_timeout(old, 999))
