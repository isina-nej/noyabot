import os
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from botapp.ai.prompts import (
    NOYA_SYSTEM_PROMPT_VERSION,
    build_ai_messages,
    build_speaker_block,
    get_creator_ids,
    get_creator_mention_html,
    get_noya_system_prompt,
    is_creator_user_id,
)
from botapp.services import call_ai_api, call_noya_api


class NoyaSystemPromptTest(TestCase):
    @override_settings(NOYA_SYSTEM_PROMPT_ENABLED=False)
    def test_build_ai_messages_respects_feature_flag(self):
        question = "سلام"
        messages = build_ai_messages(question)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("[NOW]", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn(question, messages[1]["content"])

    def test_noya_system_prompt_is_valid(self):
        self.assertEqual(NOYA_SYSTEM_PROMPT_VERSION, "v7")
        prompt = get_noya_system_prompt()
        self.assertTrue(bool(prompt))
        self.assertIn("نویا", prompt)
        self.assertIn("بانمک", prompt)
        self.assertIn("شیطون", prompt)
        self.assertIn("حاضر جواب", prompt)
        self.assertIn("خودمونی", prompt)
        self.assertIn("SERVER_CONTEXT", prompt)
        self.assertIn("CREATOR_CONFIG", prompt)
        self.assertIn("tg://user?id=", prompt)

    @patch.dict(
        os.environ,
        {
            "NOYA_CREATOR_IDS": "1399836576",
            "NOYA_CREATOR_NAME": "Sina",
            "NOYA_CREATOR_USERNAME": "sina_example",
            "ADMIN_IDS": "1,2",
        },
        clear=False,
    )
    def test_creator_identity_points_to_numeric_id(self):
        prompt = get_noya_system_prompt()
        self.assertIn("Sina", prompt)
        self.assertIn("1399836576", prompt)
        self.assertIn("tg://user?id=1399836576", prompt)
        self.assertIn("sina_example", prompt)
        self.assertEqual(get_creator_ids(), [1399836576])
        self.assertTrue(is_creator_user_id(1399836576))
        self.assertFalse(is_creator_user_id(1))
        self.assertIn("1399836576", get_creator_mention_html())

        block = build_speaker_block(speaker_user_id=1399836576, speaker_name="Sina")
        self.assertIn("role", block)

        messages = build_ai_messages("سازنده‌ات کیه؟", speaker_user_id=7, speaker_name="User")
        self.assertIn("speaker_role\": \"user\"", messages[0]["content"])

    def test_build_ai_messages_includes_system_prompt_first(self):
        question = "سلام نویا"
        messages = build_ai_messages(question)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("[NOW]", messages[0]["content"])
        self.assertIn("gregorian=", messages[0]["content"])
        self.assertIn("jalali=", messages[0]["content"])
        self.assertIn(get_noya_system_prompt()[:40], messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn(question, messages[1]["content"])

    def test_build_ai_messages_attaches_search_block(self):
        messages = build_ai_messages("قیمت دلار", search_block="[WEB]\n• دلار\n[/WEB]")
        self.assertIn("قیمت دلار", messages[-1]["content"])
        self.assertIn("دلار", messages[-1]["content"])


class NoyaSystemPromptAPITest(TestCase):
    @patch("botapp.services.httpx.AsyncClient.post")
    def test_call_noya_api_sends_system_prompt(self, mock_post):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"choices": [{"message": {"content": "خوبم"}}]}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {"NOYA_API_KEY": "test", "NOYA_CREATOR_IDS": "42", "NOYA_CREATOR_NAME": "Sina"}, clear=False):
            expected_prompt = get_noya_system_prompt()
            async_to_sync(call_noya_api)(
                "تست نویا",
                "sess-123",
                speaker_user_id=42,
                speaker_name="Sina",
            )
            payload = mock_post.call_args.kwargs["json"]
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertIn("[NOW]", payload["messages"][0]["content"])
            self.assertIn(expected_prompt[:40], payload["messages"][0]["content"])
            self.assertIn("42", expected_prompt)
            self.assertIn("speaker_role\": \"creator\"", payload["messages"][0]["content"])
            self.assertIn("تست نویا", payload["messages"][1]["content"])

    @patch("botapp.services.httpx.AsyncClient.post")
    def test_call_ai_api_sends_system_prompt(self, mock_post):
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_response.json = lambda: {"content": "خوبم"}
        mock_post.return_value = mock_response

        async_to_sync(call_ai_api)("http://api", "تست ai", "sess-123")

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("[NOW]", payload["messages"][0]["content"])
        self.assertIn(get_noya_system_prompt()[:40], payload["messages"][0]["content"])
        self.assertIn("تست ai", payload["messages"][1]["content"])


class NoyaClockAndSearchTest(TestCase):
    def test_now_block_has_both_calendars(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from botapp.noya_clock import format_now_block
        from botapp.template_renderer import gregorian_to_jalali

        now = datetime(2026, 8, 18, 5, 25, 9, tzinfo=ZoneInfo("Asia/Tehran"))
        block = format_now_block(now)
        jy, jm, jd = gregorian_to_jalali(now.date())
        self.assertIn("[NOW]", block)
        self.assertIn("Tuesday", block)
        self.assertIn("August", block)
        self.assertIn("2026", block)
        self.assertIn("05:25:09", block)
        self.assertIn(f"{jy:04d}-{jm:02d}-{jd:02d}", block)
        self.assertIn("jalali=", block)
        self.assertIn("سه‌شنبه", block)

    def test_clock_question_answers_without_model(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from botapp.noya_clock import format_clock_reply, is_clock_question

        self.assertTrue(is_clock_question("ساعت چنده"))
        self.assertTrue(is_clock_question("نویا ساعت تهران چنده"))
        self.assertTrue(is_clock_question("[درخواست فعلی]\nساعت چنده"))
        self.assertFalse(is_clock_question("خواب مفید چند تا چنده"))
        now = datetime(2026, 8, 18, 6, 40, 0, tzinfo=ZoneInfo("Asia/Tehran"))
        reply = format_clock_reply(now)
        self.assertIn("06:40", reply)
        self.assertIn("2026", reply)
        self.assertNotIn("[NOW]", reply)

        with patch("botapp.services.httpx.AsyncClient") as client:
            result = async_to_sync(call_noya_api)("ساعت چنده", "telegram:1")
        self.assertIn("تهران", result)
        client.assert_not_called()

    def test_image_prompt_extraction(self):
        from botapp.management.commands.runbot import extract_image_prompt

        self.assertEqual(extract_image_prompt("برام عکس یک گربه بکش"), "یک گربه")
        self.assertEqual(extract_image_prompt("عکس یه سیب قرمز بساز"), "یه سیب قرمز")
        self.assertEqual(extract_image_prompt("draw a flying car"), "a flying car")
        self.assertIsNone(extract_image_prompt("سلام چطوری نویا"))

    def test_search_trigger_skips_chat_and_clock(self):
        from botapp.noya_search import extract_search_query, needs_web_search

        self.assertFalse(needs_web_search("سلام خوبی؟"))
        self.assertFalse(needs_web_search("ساعت چنده"))
        self.assertFalse(needs_web_search("تاریخ امروز چیه"))
        self.assertTrue(needs_web_search("قیمت دلار الان چنده"))
        self.assertTrue(needs_web_search("اینو سرچ کن آخرین اخبار"))
        self.assertTrue(needs_web_search("سرچ بزن"))
        payload = (
            "[پیام مورد اشاره — نویا]\n[NOW]\nدوشنبه ۲۸ آبان\n\n"
            "[درخواست فعلی]\nسرچ بزن"
        )
        self.assertTrue(needs_web_search(payload))
        self.assertIn("آبان", extract_search_query(payload))

