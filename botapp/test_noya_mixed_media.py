"""Regression tests for genuine ordered multi-media and explicit native reactions.

All network/provider operations are mocked. Tool data is request-scoped.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import run_agent_loop
from botapp.agent.registry import registry
from botapp.agent.tools import (
    _react_to_chat_message, _send_media_sequence, _send_chat_messages,
    isolated_media_results, pop_media_sequence, pop_reaction_request,
)
from botapp.management.commands.runbot import (
    _deliver_noya_media_sequence, _deliver_noya_requested_reaction,
)
from botapp.noya_reply_media import requires_agent_mixed_media


def _item(kind, text, reply_to="none"):
    return {"kind": kind, "text": text, "reply_to": reply_to}


def _message():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
        send_voice=AsyncMock(return_value=SimpleNamespace(message_id=102)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=103)),
        set_message_reaction=AsyncMock(return_value=True),
    )
    message = SimpleNamespace(
        bot=bot, chat=SimpleNamespace(id=-100555, type="supergroup"),
        message_id=50, message_thread_id=7,
        reply_to_message=SimpleNamespace(
            chat=SimpleNamespace(id=-100555), message_id=42,
        ),
    )
    return message


def _fake_response(message):
    result = {"choices": [{"message": message, "finish_reason": "stop"}]}
    response = SimpleNamespace(
        status_code=200,
        json=lambda: result,
        raise_for_status=lambda: None,
        text=json.dumps(result, ensure_ascii=False),
    )
    return response


def _call(tool, args):
    return {
        "id": "call_1", "type": "function",
        "function": {
            "name": tool,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _run_agent(tool, arguments, *, scope=None):
    first = _fake_response({"role": "assistant", "content": "", "tool_calls": [
        _call(tool, arguments),
    ]})
    second = _fake_response({"role": "assistant", "content": ""})
    post = AsyncMock(side_effect=[first, second])
    with patch("httpx.AsyncClient.post", new=post):
        result = async_to_sync(run_agent_loop)(
            question="نویا انجامش بده", session_id="telegram:-100555",
            system_prompt="تو ابزارهای مناسب را انتخاب می‌کنی.",
            telegram_context=scope,
        )
    return result, post


class AgentMultimediaTests(SimpleTestCase):
    def test_composite_intent_routes_to_agent_not_literal_tts(self):
        examples = [
            "یک شعر حافظ پیدا کن بعد پیام دوم ویس شعر و پیام سوم عکس بساز",
            "نویا یک شعر پیدا کن، تو پیام دوم با صدات بخون، پیام سوم تصویر بساز",
            "Generate an image and a voice message about a bird",
        ]
        for request in examples:
            with self.subTest(request=request):
                self.assertTrue(requires_agent_mixed_media(request))
        for simple in ("با صدا بگو سلام", "عکس یک گربه بکش", "مرسی", "نویا ویس بده سلام"):
            self.assertFalse(requires_agent_mixed_media(simple))

    def test_tool_catalog_exposes_media_sequence_and_real_reaction(self):
        tools = {x["function"]["name"]: x["function"]["parameters"]
                 for x in registry.chat_definitions()}
        self.assertIn("send_media_sequence", tools)
        self.assertIn("react_to_chat_message", tools)
        for key in ("chat_id", "message_id", "file_id"):
            self.assertNotIn(key, tools["react_to_chat_message"]["properties"])

    def test_model_calls_media_tool_and_stages_actual_bytes(self):
        poem = "بیت انتخاب‌شده از شعر"
        items = [
            _item("text", poem, "current"),
            _item("voice", poem, "none"),
            _item("image", "An elegant garden inspired by Persian poetry", "previous"),
        ]
        with patch(
            "botapp.services.generate_noya_tts",
            new=AsyncMock(return_value=b"recorded-poem"),
        ) as tts, patch(
            "botapp.services.generate_noya_image",
            new=AsyncMock(return_value=b"picture-bytes"),
        ) as image:
            (answer, meta), post = _run_agent("send_media_sequence", {"items": items})
        self.assertEqual(answer, "")
        self.assertEqual(post.await_count, 2)
        self.assertEqual(meta["tools_used"], ["send_media_sequence"])
        batch = meta["queued_media_sequence"]
        self.assertEqual([x["kind"] for x in batch], ["text", "voice", "image"])
        self.assertEqual(batch[1]["bytes"], b"recorded-poem")
        self.assertEqual(batch[2]["bytes"], b"picture-bytes")
        tts.assert_awaited_once_with(poem)
        image.assert_awaited_once_with(items[2]["text"])
        self.assertEqual(post.await_args_list[0].kwargs["json"]["tool_choice"], "auto")

    def test_sequence_delivery_respects_original_order_and_replies(self):
        msg = _message()
        data = [
            _item("text", "شعر", "current"),
            dict(_item("voice", "شعر", "none"), bytes=b"audio"),
            dict(_item("image", "شعر تصویری", "previous"), bytes=b"photo"),
        ]
        with patch("botapp.management.commands.runbot.set_active_chat_action", new=AsyncMock()):
            handled = async_to_sync(_deliver_noya_media_sequence)(
                msg, {"queued_media_sequence": data},
            )
        self.assertTrue(handled)
        self.assertEqual(msg.bot.send_message.await_count, 1)
        self.assertEqual(msg.bot.send_voice.await_count, 1)
        self.assertEqual(msg.bot.send_photo.await_count, 1)
        self.assertEqual(
            msg.bot.send_message.await_args.kwargs["reply_parameters"].message_id, 50,
        )
        self.assertNotIn("reply_parameters", msg.bot.send_voice.await_args.kwargs)
        self.assertEqual(
            msg.bot.send_photo.await_args.kwargs["reply_parameters"].message_id, 102,
        )
        for call in [
            msg.bot.send_message.await_args,
            msg.bot.send_voice.await_args,
            msg.bot.send_photo.await_args,
        ]:
            self.assertEqual(call.kwargs["chat_id"], -100555)
            self.assertEqual(call.kwargs["message_thread_id"], 7)
        self.assertEqual(msg.bot.send_message.await_args.kwargs["text"], "شعر")

    def test_media_generation_failure_does_not_stage_partial_send(self):
        @isolated_media_results
        async def scenario():
            with patch(
                "botapp.services.generate_noya_tts",
                new=AsyncMock(return_value=b"tts-ok"),
            ), patch(
                "botapp.services.generate_noya_image",
                new=AsyncMock(return_value=None),
            ):
                result = await _send_media_sequence([
                    _item("text", "poem"),
                    _item("voice", "poem"),
                    _item("image", "visual of the poem"),
                ])
            return result, pop_media_sequence()
        failure, batch = async_to_sync(scenario)()
        self.assertIn("Error:", failure)
        self.assertEqual(batch, [])

    def test_composed_sequence_and_text_outbox_cannot_duplicate(self):
        @isolated_media_results
        async def scenario():
            with patch("botapp.services.generate_noya_tts", new=AsyncMock(return_value=b"abc")):
                okay = await _send_media_sequence([_item("text", "hello"), _item("voice", "hello")])
            denied = await _send_chat_messages([{"text": "hello", "reply_to": "none"}])
            return okay, denied, pop_media_sequence()
        okay, denied, batch = async_to_sync(scenario)()
        self.assertIn("آماده", okay)
        self.assertIn("Error:", denied)
        self.assertEqual(len(batch), 2)

    def test_media_sequence_is_scoped_per_concurrent_agent_invocation(self):
        @isolated_media_results
        async def work(name):
            with patch("botapp.services.generate_noya_tts", new=AsyncMock(return_value=name.encode())):
                await _send_media_sequence([_item("text", name), _item("voice", name)])
            await asyncio.sleep(0)
            return pop_media_sequence()
        async def run():
            return await asyncio.gather(work("A"), work("B"))
        a, b = async_to_sync(run)()
        self.assertEqual(a[1]["bytes"], b"A")
        self.assertEqual(b[1]["bytes"], b"B")

    def test_reaction_tool_targets_current_authenticated_message(self):
        msg = _message()
        scope = {
            "bot": msg.bot, "chat": msg.chat, "chat_id": msg.chat.id,
            "chat_type": "supergroup", "current_message_id": msg.message_id,
            "reply_to_message_id": 42,
        }
        (answer, meta), _ = _run_agent(
            "react_to_chat_message", {"emoji": "❤️", "target": "current"},
            scope=scope,
        )
        self.assertEqual(meta["queued_reaction"], {"emoji": "❤️", "target": "current"})
        self.assertEqual(answer, "")
        done = async_to_sync(_deliver_noya_requested_reaction)(msg, meta)
        self.assertTrue(done)
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["message_id"], 50)
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji, "❤️")

    def test_reaction_to_reply_stays_in_same_chat(self):
        msg = _message()
        handled = async_to_sync(_deliver_noya_requested_reaction)(
            msg, {"queued_reaction": {"emoji": "👍", "target": "replied"}},
        )
        self.assertTrue(handled)
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["message_id"], 42)
        msg.bot.set_message_reaction.reset_mock()
        msg.reply_to_message.chat.id = -100999
        with patch("botapp.management.commands.runbot.reply_noya_answer", new=AsyncMock()) as reply:
            handled = async_to_sync(_deliver_noya_requested_reaction)(
                msg, {"queued_reaction": {"emoji": "👍", "target": "replied"}},
            )
        self.assertTrue(handled)
        msg.bot.set_message_reaction.assert_not_awaited()
        reply.assert_awaited_once()

    def test_cannot_stage_reaction_without_verified_telegram_scope(self):
        @isolated_media_results
        async def work():
            return await _react_to_chat_message("❤️", "current"), pop_reaction_request()
        result, staged = async_to_sync(work)()
        self.assertIn("Error:", result)
        self.assertIsNone(staged)

    def test_invalid_first_previous_and_oversized_batch_rejected(self):
        @isolated_media_results
        async def work():
            a = await _send_media_sequence([
                _item("text", "hi", "previous"), _item("voice", "hi"),
            ])
            b = await _send_media_sequence([_item("text", "a")] * 6)
            return a, b, pop_media_sequence()
        a, b, queued = async_to_sync(work)()
        self.assertIn("Error:", a)
        self.assertIn("Error:", b)
        self.assertEqual(queued, [])
