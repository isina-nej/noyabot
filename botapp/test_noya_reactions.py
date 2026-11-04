"""Tests for Noya's human-length chat and reaction-only Telegram behavior."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.ai.prompts import get_noya_system_prompt
from botapp.noya_reactions import (
    SUGGESTED_REACTIONS,
    extract_reaction,
    reaction_eligible,
    reaction_instruction,
)


def msg(text, **kwargs):
    return SimpleNamespace(
        text=text, caption=None, edit_date=None, photo=None, sticker=None,
        animation=None, document=None, voice=None, audio=None,
        video=None, video_note=None, **kwargs,
    )


class ReactionEligibilityTests(SimpleTestCase):
    def test_short_social_replies_are_eligible(self):
        for text in ("مرسی ❤️", "دمت گرم", "نویا عالی بود", "ایول", "😂", "🥰", "@NuyaRobot باشه"):
            with self.subTest(text=text):
                self.assertTrue(reaction_eligible(msg(text)))

    def test_real_questions_requests_and_distress_must_get_text(self):
        for text in (
            "نویا", "سلام", "نویا خوبی؟", "مرسی، حالا عکس بساز",
            "نویا چرا این خطا رو میده", "ناراحتم", "درد دارم",
            "مرسی میشه توضیح بدی", "به من کمک کن", "من بتولم",
            "/start", ".", "؟", "نویا خطای 401 میگیرم",
        ):
            with self.subTest(text=text):
                self.assertFalse(reaction_eligible(msg(text)))

    def test_long_messages_media_and_edits_never_eligible(self):
        self.assertFalse(reaction_eligible(msg("مرسی " * 50)))
        message = msg("مرسی")
        message.sticker = SimpleNamespace(emoji="❤️")
        self.assertFalse(reaction_eligible(message))
        message = msg("مرسی")
        message.edit_date = object()
        self.assertFalse(reaction_eligible(message))

    @patch.dict("os.environ", {"NOYA_REACTION_ENABLED": "false"})
    def test_kill_switch(self):
        self.assertFalse(reaction_eligible(msg("مرسی")))

    def test_exact_parser_and_emoji_whitelist(self):
        for emoji in SUGGESTED_REACTIONS:
            self.assertEqual(extract_reaction(f"[[NOYA_REACT:{emoji}]]", allowed=True), emoji)
            self.assertIsNone(extract_reaction(f"[[NOYA_REACT:{emoji}]]", allowed=False))
        for text in (
            "مرسی [[NOYA_REACT:❤️]]", "باید [[NOYA_REACT:❤️]] بذارم",
            "[[NOYA_REACT:🚀]]", "[[NOYA_REACT:<script>]]",
            "[[NOYA_REACT:❤️]]\nسلام", "",
        ):
            self.assertIsNone(extract_reaction(text, allowed=True))

    def test_natural_persona_and_reaction_instruction(self):
        prompt = get_noya_system_prompt()
        self.assertIn("نه آن‌قدر کوتاه که سرد و مکانیکی", prompt)
        self.assertIn("جریان حرف را بی‌دلیل قطع نکن", prompt)
        instruction = reaction_instruction()
        self.assertIn("[[NOYA_REACT:❤️]]", instruction)
        self.assertIn("ری‌اکشن جایگزین پرسش و پاسخ نیست", instruction)


class AgentReactionPipelineTests(SimpleTestCase):
    def _agent_result(self, text, *, allowed):
        from botapp.agent.loop import run_agent_loop

        content = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
            json=content,
        )
        with patch("botapp.agent.loop.registry.chat_definitions", return_value=[]), (
            patch("botapp.agent.loop.httpx.AsyncClient") as client
        ):
            client.return_value.__aenter__.return_value.post = AsyncMock(return_value=response)
            return async_to_sync(run_agent_loop)(
                question="مرسی", session_id="telegram:123",
                system_prompt="شخصیت نویا", reaction_allowed=allowed,
            )

    def test_agent_returns_metadata_only_for_whitelisted_reaction(self):
        answer, meta = self._agent_result("[[NOYA_REACT:❤️]]", allowed=True)
        self.assertEqual(answer, "")
        self.assertEqual(meta.get("reaction_emoji"), "❤️")

    def test_agent_cannot_react_without_permission(self):
        answer, meta = self._agent_result("[[NOYA_REACT:❤️]]", allowed=False)
        self.assertIsNone(meta.get("reaction_emoji"))
        self.assertTrue(answer)

    def test_agent_regular_conversation_is_preserved(self):
        answer, meta = self._agent_result("عجب خبر خوبی! تعریف کن ببینم چی شد.", allowed=True)
        self.assertIn("تعریف کن", answer)
        self.assertFalse(meta.get("reaction_emoji"))


class TelegramReactionDeliveryTests(SimpleTestCase):
    def test_telegram_reaction_uses_original_message_and_does_not_reply(self):
        from botapp.management.commands.runbot import react_to_noya_message

        bot = SimpleNamespace(set_message_reaction=AsyncMock(return_value=True))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-100123),
            message_id=487,
            bot=bot,
        )
        success = async_to_sync(react_to_noya_message)(message, "❤️")
        self.assertTrue(success)
        bot.set_message_reaction.assert_awaited_once()
        arguments = bot.set_message_reaction.await_args.kwargs
        self.assertEqual(arguments["chat_id"], -100123)
        self.assertEqual(arguments["message_id"], 487)
        self.assertEqual(len(arguments["reaction"]), 1)
        self.assertEqual(arguments["reaction"][0].emoji, "❤️")

    def test_invalid_emoji_does_not_call_telegram(self):
        from botapp.management.commands.runbot import react_to_noya_message

        bot = SimpleNamespace(set_message_reaction=AsyncMock())
        message = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=1, bot=bot)
        self.assertFalse(async_to_sync(react_to_noya_message)(message, "🚀"))
        bot.set_message_reaction.assert_not_awaited()
