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
