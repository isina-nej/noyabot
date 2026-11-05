"""Native Telegram reaction coverage and reaction-plus-reply regressions.

No live Telegram calls, tokens, or external provider traffic.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import run_agent_loop
from botapp.agent.registry import registry
from botapp.agent.tools import (
    _react_to_chat_message,
    isolated_media_results,
    pop_reaction_request,
)
from botapp.management.commands.runbot import (
    _deliver_noya_requested_reaction,
    _deliver_noya_queued_messages,
    react_to_noya_message,
)
from botapp.noya_reactions import (
    REACTION_EMOJIS,
    SUGGESTED_REACTIONS,
    explicit_reaction_intent,
    reaction_eligible,
    reaction_instruction,
)


def _chat_message():
    bot = SimpleNamespace(
        set_message_reaction=AsyncMock(return_value=True),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=52)),
    )
    msg = SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=-100555, type="supergroup"),
        message_id=50, message_thread_id=7,
        reply_to_message=SimpleNamespace(
            message_id=41, chat=SimpleNamespace(id=-100555),
        ),
        reply=AsyncMock(),
    )
    return msg


def _scope(message, *, alone=False):
    return {
        "bot": message.bot,
        "chat": message.chat,
        "chat_id": message.chat.id,
        "chat_type": "supergroup",
        "current_message_id": 50,
        "reply_to_message_id": 41,
        "reaction_only_allowed": alone,
    }


class FullReactionsTests(SimpleTestCase):
    def test_catalog_covers_native_telegram_set_without_fake_blue_heart(self):
        from botapp.noya_reactions import UNSUPPORTED_REQUESTED_REACTIONS
        self.assertEqual(SUGGESTED_REACTIONS, REACTION_EMOJIS)
        for emoji in ("👎", "🤔", "🤯", "🐳", "💘", "💔", "❤️‍🔥", "⚡", "🕊", "🤷", "😡"):
            self.assertIn(emoji, REACTION_EMOJIS)
        self.assertNotIn("💙", REACTION_EMOJIS)
        self.assertIn("💙", UNSUPPORTED_REQUESTED_REACTIONS)
        self.assertNotIn("⭐", REACTION_EMOJIS)

        schema = {t["function"]["name"]: t["function"]["parameters"]
                  for t in registry.chat_definitions()}["react_to_chat_message"]
        self.assertTrue(REACTION_EMOJIS <= set(schema["properties"]["emoji"]["enum"]))
        self.assertIn("💙", schema["properties"]["emoji"]["enum"])
        self.assertEqual(
            schema["properties"]["mode"]["enum"], ["only", "with_reply"],
        )
        for forbidden in ("chat_id", "message_id", "user_id", "custom_emoji_id"):
            self.assertNotIn(forbidden, schema["properties"])
        self.assertIn("ری‌اکشن جایگزین پرسش و پاسخ نیست", reaction_instruction())

    def test_explicit_reaction_intent_and_social_reaction_gate(self):
        for request in (
            "نویا روی همین پیام قلب بزن",
            "یه ری‌اکشن قلب بزن",
            "ریاکشن آبی میخوام",
            "react to this message",
        ):
            self.assertTrue(explicit_reaction_intent(request))
        self.assertFalse(explicit_reaction_intent("چرا قیمت دلار بالاست؟"))
        simple = SimpleNamespace(
            text="دمت گرم", photo=None, sticker=None, animation=None,
            document=None, voice=None, audio=None, video=None,
            video_note=None, edit_date=None,
        )
        self.assertTrue(reaction_eligible(simple))
        simple.text = "چرا این خطا رو میده؟"
        self.assertFalse(reaction_eligible(simple))

    def test_reaction_only_cannot_replace_required_answer(self):
        msg = _chat_message()
        @isolated_media_results
        async def stage():
            from botapp.noya_archive_access import _SCOPE
            token = _SCOPE.set(_scope(msg, alone=False))
            try:
                output = await _react_to_chat_message("👍", "current", "only")
                return output, pop_reaction_request()
            finally:
                _SCOPE.reset(token)
        result, request = async_to_sync(stage)()
        self.assertIn("Error:", result)
        self.assertIsNone(request)

    def test_benign_reaction_only_stages_without_extra_response(self):
        msg = _chat_message()
        @isolated_media_results
        async def stage():
            from botapp.noya_archive_access import _SCOPE
            token = _SCOPE.set(_scope(msg, alone=True))
            try:
                output = await _react_to_chat_message("👎", "current", "only")
                return output, pop_reaction_request()
            finally:
                _SCOPE.reset(token)
        _, request = async_to_sync(stage)()
        self.assertEqual(request, {"emoji": "👎", "target": "current", "mode": "only"})
        metadata = {"queued_reaction": request}
        sent = async_to_sync(_deliver_noya_requested_reaction)(msg, metadata)
        self.assertTrue(sent)
        self.assertTrue(metadata["_reaction_delivery_success"])
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["message_id"], 50)
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["reaction"][0].emoji, "👎")
        msg.reply.assert_not_awaited()

    def test_blue_heart_rejected_without_silent_substitution(self):
        msg = _chat_message()
        @isolated_media_results
        async def stage():
            from botapp.noya_archive_access import _SCOPE
            token = _SCOPE.set(_scope(msg, alone=True))
            try:
                result = await _react_to_chat_message("💙", "current", "only")
                return result, pop_reaction_request()
            finally:
                _SCOPE.reset(token)
        result, request = async_to_sync(stage)()
        self.assertIn("💙", result)
        self.assertIn("Bot API", result)
        self.assertIsNone(request)
        self.assertFalse(async_to_sync(react_to_noya_message)(msg, "💙"))
        msg.bot.set_message_reaction.assert_not_awaited()

    def test_model_can_queue_reaction_with_normal_final_answer(self):
        msg = _chat_message()
        first = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "reaction", "type": "function",
             "function": {"name": "react_to_chat_message",
                          "arguments": json.dumps({"emoji": "🤩", "target": "current",
                                                   "mode": "with_reply"})}},
        ]}}]}
        second = {"choices": [{"message": {
            "role": "assistant", "content": "خیلی خوب شد! ادامه بده."
        }}]}
        def response(obj):
            return SimpleNamespace(
                status_code=200, text=json.dumps(obj, ensure_ascii=False),
                json=lambda: obj, raise_for_status=lambda: None,
            )
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=[
            response(first), response(second),
        ])):
            answer, metadata = async_to_sync(run_agent_loop)(
                question="چه خبر خوبی!",
                system_prompt="شخصیت نویا",
                session_id="telegram:-100555",
                telegram_context=_scope(msg, alone=False),
            )
        self.assertIn("ادامه", answer)
        self.assertEqual(metadata["queued_reaction"]["mode"], "with_reply")
        self.assertEqual(metadata["queued_reaction"]["emoji"], "🤩")
        handled = async_to_sync(_deliver_noya_requested_reaction)(
            msg, metadata, quiet_failure=True,
        )
        self.assertTrue(handled)
        self.assertTrue(metadata["_reaction_delivery_success"])
        msg.reply.assert_not_awaited()

    def test_with_reply_can_combine_with_staged_chat_message(self):
        msg = _chat_message()
        metadata = {
            "queued_reaction": {"emoji": "❤️", "target": "current", "mode": "with_reply"},
            "queued_chat_messages": [{"text": "منم خوشحال شدم!", "reply_to": "current"}],
        }
        async_to_sync(_deliver_noya_requested_reaction)(msg, metadata, quiet_failure=True)
        async_to_sync(_deliver_noya_queued_messages)(msg, metadata)
        self.assertEqual(msg.bot.set_message_reaction.await_count, 1)
        self.assertEqual(msg.bot.send_message.await_count, 1)
        self.assertEqual(msg.bot.send_message.await_args.kwargs["reply_parameters"].message_id, 50)
        msg.reply.assert_not_awaited()

    def test_failure_plus_normal_reply_does_not_generate_duplicate_error(self):
        msg = _chat_message()
        msg.bot.set_message_reaction.return_value = False
        meta = {"queued_reaction": {"emoji": "❤️", "target": "current", "mode": "with_reply"}}
        self.assertTrue(async_to_sync(_deliver_noya_requested_reaction)(
            msg, meta, quiet_failure=True,
        ))
        self.assertFalse(meta["_reaction_delivery_success"])
        msg.reply.assert_not_awaited()

    def test_failure_in_reaction_only_reports_error(self):
        msg = _chat_message()
        msg.bot.set_message_reaction.return_value = False
        meta = {"queued_reaction": {"emoji": "❤️", "target": "current", "mode": "only"}}
        self.assertTrue(async_to_sync(_deliver_noya_requested_reaction)(msg, meta))
        msg.reply.assert_awaited_once()

    def test_replied_reaction_never_crosses_chat(self):
        msg = _chat_message()
        meta = {"queued_reaction": {"emoji": "💘", "target": "replied", "mode": "with_reply"}}
        self.assertTrue(async_to_sync(_deliver_noya_requested_reaction)(
            msg, meta, quiet_failure=True,
        ))
        self.assertEqual(msg.bot.set_message_reaction.await_args.kwargs["message_id"], 41)
        msg.bot.set_message_reaction.reset_mock()
        msg.reply_to_message.chat.id = -100999
        self.assertTrue(async_to_sync(_deliver_noya_requested_reaction)(
            msg, meta, quiet_failure=True,
        ))
        self.assertFalse(meta["_reaction_delivery_success"])
        msg.bot.set_message_reaction.assert_not_awaited()
