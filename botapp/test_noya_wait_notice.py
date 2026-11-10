"""Regression tests: remove Noya's 60-second wait notice after final delivery.

Telegram and provider calls are mocked. A pending wait notice belongs to
one triggering user message in one chat, and is never deleted before the
final Telegram send succeeds.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.management.commands.runbot import (
    _await_noya_operation,
    _deliver_noya_queued_messages,
    _deliver_noya_media_sequence,
    _deliver_noya_agent_media,
    _handle_noya_image_generation,
    _handle_noya_image_edit,
    _handle_noya_tts,
    reply_noya_answer,
)
from botapp.noya_wait_notice import (
    _PENDING_NOTICE,
    remember_waiting_notice,
    clear_waiting_notice_after_send,
)


def _message(chat_id=-10017, request_id=301):
    chat = SimpleNamespace(id=chat_id, type="supergroup")
    bot = SimpleNamespace(
        delete_message=AsyncMock(return_value=True),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=901)),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=902)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=903)),
    )
    message = SimpleNamespace(
        chat=chat, message_id=request_id, message_thread_id=None,
        bot=bot, reply_to_message=None,
        reply=AsyncMock(return_value=SimpleNamespace(chat=chat, message_id=902)),
        reply_photo=AsyncMock(return_value=SimpleNamespace(chat=chat, message_id=903)),
        reply_voice=AsyncMock(return_value=SimpleNamespace(chat=chat, message_id=904)),
    )
    return message


def _notice(message, notice_id=850):
    return SimpleNamespace(chat=message.chat, message_id=notice_id)


class WaitingNoticeLifecycleTests(SimpleTestCase):
    def tearDown(self):
        _PENDING_NOTICE.set(None)
        super().tearDown()

    def test_one_minute_notice_deleted_only_after_main_text_send(self):
        message = _message()
        sequence = []
        notice_done = asyncio.Event()

        async def send(text, **kwargs):
            sequence.append(("send", text))
            if text == "یک لحظه صبر کن عزیزم":
                notice_done.set()
                return _notice(message)
            self.assertEqual(message.bot.delete_message.await_count, 0)
            return SimpleNamespace(chat=message.chat, message_id=905)

        async def delete_message(**kwargs):
            sequence.append(("delete", kwargs["message_id"]))
            return True

        message.reply.side_effect = send
        message.bot.delete_message.side_effect = delete_message

        async def delayed(current):
            return await current.reply("یک لحظه صبر کن عزیزم")

        async def operation():
            await notice_done.wait()
            return "پاسخ اصلی"

        async def scenario():
            with patch(
                "botapp.management.commands.runbot._noya_delayed_notice",
                new=delayed,
            ):
                answer = await _await_noya_operation(message, operation())
            self.assertEqual(answer, "پاسخ اصلی")
            self.assertEqual(message.bot.delete_message.await_count, 0)
            await reply_noya_answer(message, answer)
            # Calling the reply helper twice must never delete the old notice
            # a second time.
            await reply_noya_answer(message, "پاسخ دوم")
        async_to_sync(scenario)()

        self.assertEqual(sequence[:3], [
            ("send", "یک لحظه صبر کن عزیزم"),
            ("send", "پاسخ اصلی"),
            ("delete", 850),
        ])
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=-10017, message_id=850,
        )

    def test_quick_answer_never_sends_or_deletes_wait_notice(self):
        message = _message()
        started = asyncio.Event()

        async def delayed(_):
            started.set()
            await asyncio.Event().wait()

        async def operation():
            await started.wait()
            return "زود جواب دادم"

        async def scenario():
            with patch(
                "botapp.management.commands.runbot._noya_delayed_notice",
                new=delayed,
            ):
                answer = await _await_noya_operation(message, operation())
            await reply_noya_answer(message, answer)
        async_to_sync(scenario)()
        message.reply.assert_awaited_once()
        message.bot.delete_message.assert_not_awaited()

    def test_failed_main_send_does_not_clear_wait_notice(self):
        message = _message()
        async def scenario():
            remember_waiting_notice(message, _notice(message, 701))
            message.reply.side_effect = RuntimeError("telegram down")
            with self.assertRaises(RuntimeError):
                await reply_noya_answer(message, "اصلی")
            message.bot.delete_message.assert_not_awaited()
            message.reply.side_effect = None
            await reply_noya_answer(message, "اصلی")
        async_to_sync(scenario)()
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=701,
        )

    def test_failed_delete_never_hides_sent_reply_or_tries_twice(self):
        message = _message()
        message.bot.delete_message.side_effect = RuntimeError("cannot delete")
        async def scenario():
            remember_waiting_notice(message, _notice(message, 702))
            await reply_noya_answer(message, "جواب رسید")
            await reply_noya_answer(message, "پیام بعدی")
        async_to_sync(scenario)()
        self.assertEqual(message.reply.await_count, 2)
        message.bot.delete_message.assert_awaited_once()

    def test_wrong_chat_or_request_cannot_delete_a_waiting_message(self):
        message = _message()
        other_chat = _message(chat_id=-10018, request_id=301)
        other_request = _message(chat_id=-10017, request_id=302)

        async def scenario():
            remember_waiting_notice(message, _notice(message, 703))
            self.assertFalse(await clear_waiting_notice_after_send(other_chat))
            self.assertFalse(await clear_waiting_notice_after_send(other_request))
            await reply_noya_answer(message, "اصلی")
        async_to_sync(scenario)()
        other_chat.bot.delete_message.assert_not_awaited()
        other_request.bot.delete_message.assert_not_awaited()
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=703,
        )

    def test_overlapping_requests_keep_separate_notice_ids(self):
        first = _message(chat_id=-10017, request_id=500)
        second = _message(chat_id=-10017, request_id=501)
        async def run_one(message, id_):
            remember_waiting_notice(message, _notice(message, id_))
            await asyncio.sleep(0)
            await reply_noya_answer(message, "جواب")
        async def scenario():
            await asyncio.gather(
                run_one(first, 800), run_one(second, 801),
            )
        async_to_sync(scenario)()
        first.bot.delete_message.assert_awaited_once_with(
            chat_id=-10017, message_id=800,
        )
        second.bot.delete_message.assert_awaited_once_with(
            chat_id=-10017, message_id=801,
        )

    def test_multi_message_outbox_cleanup_after_successful_delivery(self):
        message = _message()
        async def scenario():
            remember_waiting_notice(message, _notice(message, 710))
            delivered = await _deliver_noya_queued_messages(message, {
                "queued_chat_messages": [
                    {"text": "اول", "reply_to": "current"},
                    {"text": "دوم", "reply_to": "previous"},
                ],
            })
            self.assertTrue(delivered)
        async_to_sync(scenario)()
        self.assertEqual(message.bot.send_message.await_count, 2)
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=710,
        )

    def test_mixed_media_sequence_cleanup_only_after_send(self):
        message = _message()
        async def scenario():
            remember_waiting_notice(message, _notice(message, 711))
            delivered = await _deliver_noya_media_sequence(message, {
                "queued_media_sequence": [
                    {"kind": "text", "text": "متن نهایی", "reply_to": "current"},
                ],
            })
            self.assertTrue(delivered)
        async_to_sync(scenario)()
        message.bot.send_message.assert_awaited_once()
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=711,
        )

    def test_agent_image_and_voice_cleanup_after_actual_media_send(self):
        message = _message()
        async def scenario():
            remember_waiting_notice(message, _notice(message, 712))
            delivered = await _deliver_noya_agent_media(message, "", {
                "generated_image_b64": "aW1hZ2U=",
                "generated_tts_audio": b"audio",
            })
            self.assertTrue(delivered)
        async_to_sync(scenario)()
        message.reply_photo.assert_awaited_once()
        message.reply_voice.assert_awaited_once()
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=712,
        )

    def test_direct_image_generation_cleanup_after_photo_send(self):
        message = _message()
        async def await_fake(current, operation):
            result = await operation
            remember_waiting_notice(current, _notice(current, 713))
            return result

        async def scenario():
            with patch(
                "botapp.management.commands.runbot._await_noya_operation",
                new=await_fake,
            ), patch(
                "botapp.management.commands.runbot.generate_noya_image",
                new=AsyncMock(return_value=b"fakePNG"),
            ):
                await _handle_noya_image_generation.__wrapped__(
                    message, "یک منظره",
                )
        async_to_sync(scenario)()
        message.reply_photo.assert_awaited_once()
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=message.chat.id, message_id=713,
        )

    def test_direct_image_edit_and_tts_cleanup(self):
        message = _message()
        async def await_fake(current, operation):
            result = await operation
            remember_waiting_notice(current, _notice(current, 714))
            return result

        async def scenario():
            with patch(
                "botapp.management.commands.runbot._await_noya_operation",
                new=await_fake,
            ), patch(
                "botapp.management.commands.runbot.edit_noya_image",
                new=AsyncMock(return_value=b"PNGbytes"),
            ), patch(
                "botapp.management.commands.runbot.generate_noya_tts",
                new=AsyncMock(return_value=b"fake mp3"),
            ):
                await _handle_noya_image_edit.__wrapped__(
                    message, "رنگ رو عوض کن", b"source",
                )
                remember_waiting_notice(message, _notice(message, 715))
                await _handle_noya_tts.__wrapped__(message, "سلام")
        async_to_sync(scenario)()
        message.reply_photo.assert_awaited_once()
        message.reply_voice.assert_awaited_once()
        self.assertEqual(message.bot.delete_message.await_count, 2)
        self.assertEqual(
            [call.kwargs["message_id"]
             for call in message.bot.delete_message.await_args_list],
            [714, 714],
        )

    def test_wait_notice_send_fail_does_not_register_cleanup(self):
        message = _message()
        async def scenario():
            remember_waiting_notice(message, None)
            await reply_noya_answer(message, "جواب رسید")
        async_to_sync(scenario)()
        message.bot.delete_message.assert_not_awaited()
