"""End-to-end Noya autonomous media tool regressions, without network or bot tokens."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import run_agent_loop
from botapp.agent.registry import registry
from botapp.management.commands.runbot import _deliver_noya_agent_media


class _FakeResponse:
    status_code = 200

    def __init__(self, message):
        self.body = {"choices": [{"message": message, "finish_reason": "stop"}]}

    @property
    def text(self):
        return json.dumps(self.body)

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


def _tool_call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}


def _agent(**kwargs):
    defaults = {
        "question": "نویا یک تصویر بکش و یک ویس هم بفرست",
        "session_id": "telegram:-1001",
        "system_prompt": "فقط برای درخواست واقعی کاربر از ابزارها استفاده کن",
    }
    defaults.update(kwargs)
    return async_to_sync(run_agent_loop)(**defaults)


class AutonomousToolTests(SimpleTestCase):
    def test_model_receives_usable_chat_media_tools_not_admin_tools(self):
        defs = {x["function"]["name"]: x for x in registry.chat_definitions()}
        for name in ("generate_image", "edit_image", "text_to_speech", "web_search", "get_time"):
            self.assertIn(name, defs)
        self.assertNotIn("ban_member", defs)
        self.assertIn("instruction", defs["edit_image"]["function"]["parameters"]["properties"])

    def test_model_can_choose_image_and_voice_in_one_turn(self):
        first = _FakeResponse({"role": "assistant", "content": "", "tool_calls": [
            _tool_call("generate_image", {"prompt": "a little blue bird"}, "img"),
            _tool_call("text_to_speech", {"text": "سلام!"}, "tts"),
        ]})
        second = _FakeResponse({"role": "assistant", "content": "عکس و ویس رو فرستادم"})
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=[first, second])) as post, patch(
            "botapp.services.generate_noya_image", new=AsyncMock(return_value=b"picture")
        ) as image, patch(
            "botapp.services.generate_noya_tts", new=AsyncMock(return_value=b"voice")
        ) as voice:
            text, meta = _agent()
        self.assertEqual(text, "عکس و ویس رو فرستادم")
        self.assertEqual(base64.b64decode(meta["generated_image_b64"]), b"picture")
        self.assertEqual(meta["generated_tts_audio"], b"voice")
        self.assertEqual(meta["tools_used"], ["generate_image", "text_to_speech"])
        self.assertEqual(post.await_count, 2)
        image.assert_awaited_once()
        voice.assert_awaited_once()
        payload = post.await_args_list[0].kwargs["json"]
        self.assertEqual(payload["tool_choice"], "auto")

    def test_model_can_edit_attached_reply_image_without_slash_command(self):
        photo = b"original photo"
        input_image = [{"mime": "image/jpeg", "data": photo, "source": "reply"}]
        first = _FakeResponse({"role": "assistant", "content": "", "tool_calls": [
            _tool_call("edit_image", {"instruction": "make background pale blue"}, "edit1"),
        ]})
        second = _FakeResponse({"role": "assistant", "content": "اینم نسخه ویرایش‌شده"})
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=[first, second])), patch(
            "botapp.services.edit_noya_image", new=AsyncMock(return_value=b"edited photo")
        ) as edit:
            _, meta = _agent(question="پس زمینه این عکس رو آبی روشن کن", images=input_image)
        edit.assert_awaited_once_with("make background pale blue", photo, "image/jpeg")
        self.assertEqual(base64.b64decode(meta["generated_image_b64"]), b"edited photo")

    def test_edit_tool_cannot_use_an_image_from_someone_else_or_any_file_path(self):
        first = _FakeResponse({"role": "assistant", "content": "", "tool_calls": [
            _tool_call("edit_image", {"instruction": "edit /etc/passwd"}, "edit1"),
        ]})
        second = _FakeResponse({"role": "assistant", "content": "عکسی دریافت نکردم"})
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=[first, second])), patch(
            "botapp.services.edit_noya_image", new=AsyncMock()
        ) as edit:
            _, meta = _agent(question="این تصویر رو عوض کن", images=None)
        edit.assert_not_awaited()
        self.assertNotIn("generated_image_b64", meta)

    def test_telegram_delivery_sends_both_generated_files(self):
        message = SimpleNamespace(
            reply_photo=AsyncMock(), reply_voice=AsyncMock(), reply=AsyncMock(),
        )
        result = {
            "generated_image_b64": base64.b64encode(b"photo").decode("ascii"),
            "generated_tts_audio": b"voice",
        }
        with patch(
            "botapp.management.commands.runbot.set_active_chat_action",
            new=AsyncMock(return_value=True)
        ), patch("botapp.management.commands.runbot.reply_noya_answer", new=AsyncMock()) as text:
            handled = async_to_sync(_deliver_noya_agent_media)(message, "اینم", result)
        self.assertTrue(handled)
        message.reply_photo.assert_awaited_once()
        message.reply_voice.assert_awaited_once()
        self.assertEqual(message.reply_photo.await_args.kwargs["caption"], "اینم")
        text.assert_not_awaited()

    def test_no_media_returns_false_for_text_fallback(self):
        message = SimpleNamespace(reply_photo=AsyncMock(), reply_voice=AsyncMock())
        result = async_to_sync(_deliver_noya_agent_media)(message, "جواب", {})
        self.assertFalse(result)
        message.reply_photo.assert_not_awaited()
        message.reply_voice.assert_not_awaited()

    def test_invalid_image_payload_does_not_block_valid_voice(self):
        message = SimpleNamespace(
            reply_photo=AsyncMock(), reply_voice=AsyncMock(),
        )
        result = {
            "generated_image_b64": "invalid@base64",
            "generated_tts_audio": b"voice",
        }
        with patch(
            "botapp.management.commands.runbot.set_active_chat_action",
            new=AsyncMock(return_value=True)
        ), patch(
            "botapp.management.commands.runbot.reply_noya_answer",
            new=AsyncMock(),
        ):
            handled = async_to_sync(_deliver_noya_agent_media)(message, "", result)
        self.assertTrue(handled)
        message.reply_photo.assert_not_awaited()
        message.reply_voice.assert_awaited_once()

    def test_media_without_final_text_keeps_valid_result(self):
        from botapp.services import call_noya_api
        with patch(
            "botapp.services.is_clock_question", return_value=False,
        ), patch(
            "botapp.agent.loop.run_agent_loop",
            new=AsyncMock(return_value=("", {"generated_tts_audio": b"audio"})),
        ):
            text, meta = async_to_sync(call_noya_api)(
                "با صدات جواب بده", "telegram:123",
            )
        self.assertEqual(text, "")
        self.assertEqual(meta["generated_tts_audio"], b"audio")
