"""Noya same-chat multi-message/reply loop regressions (no network)."""
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
    _send_chat_messages,
    isolated_media_results,
    pop_chat_outbox,
)
from botapp.management.commands.runbot import _deliver_noya_queued_messages


def _item(text, mode="current"):
    return {"text": text, "reply_to": mode}


class _Response:
    status_code = 200

    def __init__(self, message):
        self._body = {"choices": [{"message": message, "finish_reason": "stop"}]}

    @property
    def text(self):
        return json.dumps(self._body, ensure_ascii=False)

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _call(messages, index):
    return {
        "type": "function",
        "id": f"c{index}",
        "function": {
            "name": "send_chat_messages",
            "arguments": json.dumps({"messages": messages}, ensure_ascii=False),
        },
    }


def _ask(responses):
    post = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.post", new=post):
        result = async_to_sync(run_agent_loop)(
            question="نویا سه تا پیام جدا بفرست",
            session_id="telegram:-10",
            system_prompt="برای چند پیام از ابزار استفاده کن.",
        )
    return result, post


def _tg_message(*, chat_id=-1008, topic=12, response_ids=(101, 102, 103)):
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=[
            SimpleNamespace(message_id=mid) for mid in response_ids
        ]),
    )
    message = SimpleNamespace(
        bot=bot, chat=SimpleNamespace(id=chat_id),
        message_id=50, message_thread_id=topic,
        reply=AsyncMock(),
    )
    return message


class NoyaMultiMessageTests(SimpleTestCase):
    def test_model_selects_bounded_multi_message_tool(self):
        first = _Response({
            "role": "assistant",
            "content": "",
            "tool_calls": [_call([
                _item("اول", "current"),
                _item("دوم", "previous"),
                _item("سوم", "none"),
            ], 1)],
        })
        second = _Response({"role": "assistant", "content": "انجام شد"})
        (text, metadata), post = _ask([first, second])
        self.assertEqual(post.await_count, 2)
        self.assertEqual(text, "انجام شد")
        self.assertEqual(metadata["tools_used"], ["send_chat_messages"])
        self.assertEqual(metadata["queued_chat_messages"], [
            _item("اول", "current"),
            _item("دوم", "previous"),
            _item("سوم", "none"),
        ])
        self.assertEqual(post.await_args_list[0].kwargs["json"]["tool_choice"], "auto")

    def test_telegram_delivers_reply_chain_and_independent_messages_in_one_topic(self):
        message = _tg_message()
        metadata = {
            "queued_chat_messages": [
                _item("اول", "current"),
                _item("دوم", "previous"),
                _item("<b>سوم</b>", "none"),
            ],
        }
        handled = async_to_sync(_deliver_noya_queued_messages)(message, metadata)
        self.assertTrue(handled)
        self.assertEqual(message.bot.send_message.await_count, 3)
        c0, c1, c2 = [x.kwargs for x in message.bot.send_message.await_args_list]
        self.assertEqual(c0["reply_parameters"].message_id, 50)
        self.assertEqual(c1["reply_parameters"].message_id, 101)
        self.assertNotIn("reply_parameters", c2)
        for item in (c0, c1, c2):
            self.assertEqual(item["chat_id"], -1008)
            self.assertEqual(item["message_thread_id"], 12)
            self.assertIsNone(item["parse_mode"])
        self.assertEqual(c2["text"], "<b>سوم</b>")
        message.reply.assert_not_awaited()

    def test_multiple_llm_tool_rounds_accumulate_into_one_outbox(self):
        a = _Response({"role": "assistant", "content": "", "tool_calls": [
            _call([_item("یک", "none")], 1),
        ]})
        b = _Response({"role": "assistant", "content": "", "tool_calls": [
            _call([_item("دو", "previous")], 2),
        ]})
        c = _Response({"role": "assistant", "content": ""})
        (text, metadata), post = _ask([a, b, c])
        self.assertEqual(post.await_count, 3)
        self.assertEqual(text, "")
        self.assertEqual(metadata["queued_chat_messages"], [
            _item("یک", "none"), _item("دو", "previous"),
        ])

    def test_turn_limit_applies_across_agent_iterations(self):
        six = [_item(f"پیام {i}", "none") for i in range(6)]
        a = _Response({"role": "assistant", "content": "", "tool_calls": [
            _call(six, 1),
        ]})
        b = _Response({"role": "assistant", "content": "", "tool_calls": [
            _call([_item("هفتم", "none")], 2),
        ]})
        c = _Response({"role": "assistant", "content": ""})
        (_, metadata), _ = _ask([a, b, c])
        self.assertEqual(metadata["queued_chat_messages"], six)

    def test_untrusted_model_cannot_select_arbitrary_chat_or_message_ids(self):
        schema = {t["function"]["name"]: t["function"]["parameters"]
                  for t in registry.chat_definitions()}["send_chat_messages"]
        self.assertFalse(schema["items"]["additionalProperties"] if "items" in schema else
                         schema["properties"]["messages"]["items"]["additionalProperties"])
        @isolated_media_results
        async def check():
            out = await registry.dispatch_async(
                "send_chat_messages",
                {"messages": [{"text": "secret", "reply_to": "none", "chat_id": 123}]},
            )
            assert "invalid input" in out.lower()
            assert pop_chat_outbox() == []
        async_to_sync(check)()

    def test_tool_rejects_outside_active_request(self):
        result = async_to_sync(_send_chat_messages)([_item("Hello", "none")])
        self.assertIn("Error", result)

    def test_atomic_invalid_batch_and_no_cross_turn_leak(self):
        @isolated_media_results
        async def run(messages):
            result = await _send_chat_messages(messages)
            return result, pop_chat_outbox()
        err, left = async_to_sync(run)([
            _item("OK", "none"), _item(" ", "previous"),
        ])
        self.assertIn("Error", err)
        self.assertEqual(left, [])
        _, sent = async_to_sync(run)([_item("Fresh", "none")])
        self.assertEqual(sent, [_item("Fresh", "none")])

    def test_previous_reply_requires_successful_predecessor(self):
        message = _tg_message()
        with patch("botapp.management.commands.runbot.reply_noya_answer", new=AsyncMock()) as fallback:
            handled = async_to_sync(_deliver_noya_queued_messages)(message, {
                "queued_chat_messages": [_item("بی‌مرجع", "previous")],
            })
        self.assertTrue(handled)
        message.bot.send_message.assert_not_awaited()
        fallback.assert_awaited_once()

    def test_partial_send_failure_does_not_repeat_or_expand_destinations(self):
        message = _tg_message()
        message.bot.send_message = AsyncMock(side_effect=[
            OSError("transient"), SimpleNamespace(message_id=201),
        ])
        handled = async_to_sync(_deliver_noya_queued_messages)(message, {
            "queued_chat_messages": [
                _item("first", "none"),
                _item("second", "current"),
            ],
        })
        self.assertTrue(handled)
        self.assertEqual(message.bot.send_message.await_count, 2)
        self.assertEqual(
            message.bot.send_message.await_args_list[1].kwargs["reply_parameters"].message_id, 50,
        )

    def test_successful_outbox_with_empty_final_model_text_has_no_fallback_error(self):
        from botapp.services import call_noya_api
        with patch("botapp.services.is_clock_question", return_value=False), patch(
            "botapp.agent.loop.run_agent_loop",
            new=AsyncMock(return_value=("", {
                "queued_chat_messages": [_item("سلام", "none")],
            })),
        ):
            text, metadata = async_to_sync(call_noya_api)(
                "سه تا پیام جدا بفرست", "telegram:-10",
            )
        self.assertEqual(text, "")
        self.assertEqual(metadata["queued_chat_messages"], [_item("سلام", "none")])

    def test_concurrent_turn_outboxes_do_not_mix(self):
        @isolated_media_results
        async def turn(label):
            await _send_chat_messages([_item(label, "none")])
            await asyncio.sleep(0.01 if label == "A" else 0)
            return pop_chat_outbox()
        async def run():
            return await asyncio.gather(turn("A"), turn("B"))
        a, b = async_to_sync(run)()
        self.assertEqual(a, [_item("A", "none")])
        self.assertEqual(b, [_item("B", "none")])
