from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from botapp.nouya_handler import (
    _channel_question,
    _guest_question,
    channel_nouya_handler,
    guest_nouya_handler,
    router,
)


class GuestBotTest(SimpleTestCase):
    def test_guest_question_removes_bot_mention_and_keeps_reply_context(self):
        message = SimpleNamespace(
            text="@bot این را بررسی کن",
            caption=None,
            reply_to_message=SimpleNamespace(
                text="متن قبلی",
                caption=None,
                from_user=SimpleNamespace(id=1, full_name="User", first_name="User", username="u"),
                reply_to_message=None,
            ),
            chat=SimpleNamespace(id=-1, type="private"),
            message_id=1,
            from_user=None,
        )

        payload = _guest_question(message, "bot")
        self.assertIn("متن قبلی", payload)
        self.assertIn("[درخواست فعلی]" + "\n" + "این را بررسی کن", payload)
        self.assertIn("[پیام مورد اشاره", payload)


    def test_guest_handler_answers_guest_query_with_model_result(self):
        answer_guest_query = AsyncMock(
            return_value=SimpleNamespace(inline_message_id="inline-123"),
        )
        edit_message_text = AsyncMock()
        message = SimpleNamespace(
            guest_query_id="guest-123",
            text="@NuyaRobot سلام",
            caption=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=-100),
            message_id=42,
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot")),
                edit_message_text=edit_message_text,
            ),
            answer_guest_query=answer_guest_query,
        )

        with patch(
            "botapp.nouya_handler.call_noya_api",
            new=AsyncMock(return_value=("سلام، من نویا هستم.", {})),
        ) as call_ai:
            import asyncio

            asyncio.run(guest_nouya_handler(message))

        call_ai.assert_awaited_once_with(
            "سلام",
            session_id="telegram:guest:-100:42",
            images=None,
        )
        answer_guest_query.assert_awaited_once()
        progress = answer_guest_query.await_args.kwargs["result"]
        self.assertEqual(progress.type, "article")
        self.assertEqual(progress.input_message_content.message_text, "یک لحظه…")
        edit_message_text.assert_awaited_once_with(
            inline_message_id="inline-123",
            text="سلام، من نویا هستم.",
            parse_mode="HTML",
        )

    def test_channel_question_targets_mention_or_name_only(self):
        self.assertEqual(_channel_question("@NuyaRobot امروز چه خبر؟", "NuyaRobot"), ("امروز چه خبر؟", False))
        self.assertEqual(_channel_question("نویا این را خلاصه کن", "NuyaRobot"), ("این را خلاصه کن", False))
        self.assertEqual(_channel_question("یک پست عادی", "NuyaRobot"), (None, False))
        self.assertEqual(_channel_question("نویا", "NuyaRobot"), ("", True))
        self.assertEqual(_channel_question("@NuyaRobot", "NuyaRobot"), ("", True))

    def test_channel_handler_replies_to_targeted_post(self):
        message = SimpleNamespace(
            text="@NuyaRobot سلام",
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot")),
            ),
            chat=SimpleNamespace(id=-100),
            message_id=77,
            reply=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        )
        captured = {}

        async def _fake_run(_message, payload, provider, session_id, **kwargs):
            captured["session_id"] = session_id
            return ("پاسخ کانال", {})

        with patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(side_effect=_fake_run),
        ) as run_memory:
            import asyncio

            asyncio.run(channel_nouya_handler(message))
        run_memory.assert_awaited_once()
        self.assertIn("channel", captured["session_id"])
        message.reply.assert_awaited_once_with("یک لحظه…")
        message.reply.return_value.edit_text.assert_awaited_once_with(
            "پاسخ کانال",
            parse_mode="HTML",
        )

    def test_channel_handler_replies_canned_on_bare_name(self):
        message = SimpleNamespace(
            text="نویا",
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot")),
            ),
            chat=SimpleNamespace(id=-100),
            message_id=78,
            reply=AsyncMock(),
        )
        import asyncio
        with patch("botapp.nouya_handler.run_ai_with_memory") as run_memory:
            asyncio.run(channel_nouya_handler(message))
        run_memory.assert_not_awaited()
        message.reply.assert_awaited_once()
        from botapp.nouya_handler import RESPONSES
        self.assertIn(message.reply.call_args[0][0], RESPONSES)

    def test_channel_handler_analysis_reports_mtproto_unavailable(self):
        progress_mock = SimpleNamespace(
            edit_text=AsyncMock(),
        )
        message = SimpleNamespace(
            text="نویا تحلیل",
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot")),
            ),
            chat=SimpleNamespace(id=-100),
            message_id=79,
            reply=AsyncMock(return_value=progress_mock),
        )
        from botapp.channel_insights import ChannelInsightUnavailable
        with patch(
            "botapp.nouya_handler.fetch_channel_digest",
            side_effect=ChannelInsightUnavailable("mtproto_not_configured"),
        ), patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(return_value=("تحلیل پست انجام شد", {})),
        ) as run_memory:
            import asyncio
            asyncio.run(channel_nouya_handler(message))
        run_memory.assert_not_awaited()
        progress_mock.edit_text.assert_awaited_once()

    def test_guest_update_observer_is_registered(self):
        self.assertIn("guest_message", router.resolve_used_update_types())
        self.assertIn("channel_post", router.resolve_used_update_types())

    def test_channel_handler_ignores_regular_post(self):
        message = SimpleNamespace(
            text="یک پست عادی",
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot")),
            ),
            reply=AsyncMock(),
        )
        import asyncio

        asyncio.run(channel_nouya_handler(message))
        message.reply.assert_not_awaited()
