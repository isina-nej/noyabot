"""No-network regressions for Noya's ongoing Telegram typing/voice statuses."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.noya_chat_actions import (
    NoyaChatAction,
    set_active_chat_action,
    show_chat_action,
    with_chat_action,
)


class _Bot:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def send_chat_action(self, **kwargs):
        self.events.append(kwargs)
        if self.fail:
            raise RuntimeError("forbidden action")


def _message(bot=None, *, chat_id=100, thread_id=None):
    return SimpleNamespace(
        bot=bot or _Bot(),
        chat=SimpleNamespace(id=chat_id),
        message_thread_id=thread_id,
        reply=AsyncMock(),
        reply_voice=AsyncMock(),
    )


def _run(coro):
    return async_to_sync(coro)()


class NoyaChatActionTests(SimpleTestCase):
    def test_refreshes_typing_until_reply_completed_then_stops(self):
        async def scenario():
            msg = _message()
            @with_chat_action("typing")
            async def handler(message):
                await asyncio.sleep(0.055)
                await message.reply("سلام")
            with patch("botapp.noya_chat_actions._REFRESH_SECONDS", 0.015):
                await handler(msg)
            n = len(msg.bot.events)
            self.assertGreaterEqual(n, 2)
            self.assertTrue(all(e["action"] == "typing" for e in msg.bot.events))
            msg.reply.assert_awaited_once_with("سلام")
            await asyncio.sleep(0.045)
            self.assertEqual(n, len(msg.bot.events))
        _run(scenario)

    def test_voice_recording_then_upload_action_immediate(self):
        async def scenario():
            msg = _message()
            async with show_chat_action(msg, "record_voice"):
                self.assertEqual(msg.bot.events[-1]["action"], "record_voice")
                self.assertTrue(await set_active_chat_action("upload_voice"))
                self.assertEqual(msg.bot.events[-1]["action"], "upload_voice")
                await msg.reply_voice(voice=b"audio")
            self.assertEqual(
                [e["action"] for e in msg.bot.events],
                ["record_voice", "upload_voice"],
            )
            self.assertFalse(await set_active_chat_action("typing"))
        _run(scenario)

    def test_nested_voice_in_typing_reuses_one_sender_without_restoring_typing(self):
        async def scenario():
            msg = _message()
            async with show_chat_action(msg):
                async with show_chat_action(msg, "record_voice") as nested:
                    self.assertEqual(nested.action, "record_voice")
                    await set_active_chat_action("upload_voice")
                self.assertEqual(msg.bot.events[-1]["action"], "upload_voice")
            self.assertEqual(
                [e["action"] for e in msg.bot.events],
                ["typing", "record_voice", "upload_voice"],
            )
        _run(scenario)

    def test_topic_id_is_forwarded_for_forum_chat(self):
        async def scenario():
            msg = _message(chat_id=-10055, thread_id=12)
            async with show_chat_action(msg):
                pass
            self.assertEqual(msg.bot.events, [
                {"chat_id": -10055, "action": "typing", "message_thread_id": 12},
            ])
        _run(scenario)

    def test_failed_telegram_status_does_not_stop_reply(self):
        async def scenario():
            msg = _message(bot=_Bot(fail=True))
            @with_chat_action("typing")
            async def handler(message):
                await message.reply("The answer was sent")
            await handler(msg)
            msg.reply.assert_awaited_once()
        _run(scenario)

    def test_cancelled_handler_cleans_up_refresh_task(self):
        async def scenario():
            msg = _message()
            started = asyncio.Event()
            @with_chat_action("typing")
            async def handler(message):
                started.set()
                await asyncio.Event().wait()
            task = asyncio.create_task(handler(msg))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            count = len(msg.bot.events)
            await asyncio.sleep(0.03)
            self.assertEqual(count, len(msg.bot.events))
        _run(scenario)

    def test_concurrent_chats_do_not_share_voice_status(self):
        async def scenario():
            bot = _Bot()
            msg1 = _message(bot=bot, chat_id=1)
            msg2 = _message(bot=bot, chat_id=2)
            async def first():
                async with show_chat_action(msg1):
                    await set_active_chat_action("record_voice")
                    await asyncio.sleep(0.012)
            async def second():
                async with show_chat_action(msg2):
                    await asyncio.sleep(0.012)
            await asyncio.gather(first(), second())
            first_events = [v["action"] for v in bot.events if v["chat_id"] == 1]
            second_events = [v["action"] for v in bot.events if v["chat_id"] == 2]
            self.assertEqual(first_events, ["typing", "record_voice"])
            self.assertEqual(second_events, ["typing"])
        _run(scenario)

    def test_actual_tts_handler_shows_record_voice_then_upload_voice(self):
        async def scenario():
            from botapp.management.commands.runbot import _handle_noya_tts
            msg = _message()
            with patch(
                "botapp.management.commands.runbot.generate_noya_tts",
                new=AsyncMock(return_value=b"mp3-audio"),
            ):
                await _handle_noya_tts(msg, "درود")
            msg.reply_voice.assert_awaited_once()
            self.assertEqual(
                [x["action"] for x in msg.bot.events],
                ["record_voice", "upload_voice"],
            )
        _run(scenario)

    def test_tts_agent_tool_switches_active_request_to_record_voice(self):
        async def scenario():
            from botapp.agent.tools import _text_to_speech
            msg = _message()
            with patch(
                "botapp.services.generate_noya_tts",
                new=AsyncMock(return_value=b"audio"),
            ):
                async with show_chat_action(msg):
                    result = await _text_to_speech("سلام")
            self.assertIn("موفقیت", result)
            self.assertEqual(
                [x["action"] for x in msg.bot.events],
                ["typing", "record_voice"],
            )
        _run(scenario)
