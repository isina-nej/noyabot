"""Vision adapter tests: real response shapes, safe diagnostics and fallback.

All traffic is mocked; image bytes, bearer credentials and file paths never
leave the test process. This prevents a model failure being mislabeled as an
archived Telegram message failure.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings

from botapp.noya_vision_client import (
    _unpack_vision_content, read_vision_frames,
)


def _response(content, *, finish_reason="stop", reasoning_tokens=0):
    response = MagicMock()
    response.status_code = 200
    response.text = ""
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }
    return response


def _env(extra=None):
    return {
        "NOYA_API_URL": "https://vision.example/v1/chat/completions",
        "NOYA_API_KEY": "PRIVATE-TEST-KEY",
        "NOYA_VISION_MODEL": "vision-primary",
        "NOYA_TOOL_MODEL": "text-tool",
        "NOYA_VISION_FALLBACK_MODEL": "",
        "NOYA_VISION_REASONING_EFFORT": "",
        **(extra or {}),
    }


def _run(post_side_effect=None, *, extra=None):
    with patch.dict(os.environ, _env(extra)), patch(
        "botapp.noya_vision_client.httpx.AsyncClient"
    ) as transport:
        post = transport.return_value.__aenter__.return_value.post
        if post_side_effect is not None:
            if isinstance(post_side_effect, list):
                post.side_effect = post_side_effect
            else:
                post.return_value = post_side_effect
        else:
            post.return_value = _response("روی عکس نوشته امیدوار باش")
        result = async_to_sync(read_vision_frames)(
            [("image/jpeg", b"JPEGbytes")],
            "نوشته‌های داخل عکس رو بخون",
            max_chars=1900,
        )
        calls = [item.kwargs["json"] for item in post.await_args_list]
        return result, calls


class VisionProviderResponseTests(SimpleTestCase):
    def test_plain_text_and_image_passed_to_selected_vision_model(self):
        result, calls = _run()
        self.assertIn("امیدوار", result.text)
        self.assertEqual(result.model, "vision-primary")
        self.assertEqual(calls[0]["model"], "vision-primary")
        self.assertGreaterEqual(calls[0]["max_tokens"], 1600)
        content = calls[0]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertFalse("reasoning_effort" in calls[0])

    def test_reasoning_effort_can_be_explicitly_set_low(self):
        answer, calls = _run(extra={"NOYA_VISION_REASONING_EFFORT": "low"})
        self.assertTrue(answer.text)
        self.assertEqual(calls[0]["reasoning_effort"], "low")

    def test_multimodal_provider_content_blocks_are_parsed(self):
        response = _response([
            {"type": "text", "text": "پایان"},
            {"type": "output_text", "text": "اندوه"},
            {"type": "unknown", "text": "not user-visible"},
        ])
        result, _ = _run(response)
        self.assertEqual(result.text, "پایان اندوه")
        self.assertNotIn("not user-visible", result.text)

    def test_9router_sse_stream_is_reassembled_even_with_stream_false(self):
        payload = (
            'data: {"choices":[{"delta":{"content":"متن "}}]}\\n\\n'
            'data: {"choices":[{"delta":{"reasoning_content":"SECRET"}},{"index":0}]}\\n\\n'
            'data: {"choices":[{"delta":{"content":"فال"}}]}\\n\\n'
            'data: [DONE]\\n\\n'
        ).replace("\\n", "\n")
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("non JSON")
        response.text = payload
        read, _ = _run(response)
        self.assertEqual(read.text, "متن فال")
        self.assertNotIn("SECRET", read.text)

    def test_empty_visible_content_has_specific_reason_instead_of_fake_ocr(self):
        response = _response(None, finish_reason="length", reasoning_tokens=1200)
        with self.assertLogs("botapp.noya_vision_client", "WARNING") as logs:
            result, calls = _run(response)
        self.assertEqual(result.text, "")
        self.assertEqual(result.reason, "empty_content_length")
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("reasoning_tokens=1200" in msg for msg in logs.output))
        self.assertFalse(any("PRIVATE-TEST-KEY" in msg for msg in logs.output))

    def test_http_401_is_explained_and_does_not_retry_credentials(self):
        request = httpx.Request("POST", "https://vision.example/v1/chat/completions")
        error_response = httpx.Response(401, request=request)
        response = MagicMock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "unauthorized", request=request, response=error_response,
        )
        with self.assertLogs("botapp.noya_vision_client", "WARNING") as logs:
            result, calls = _run([response, _response("backup")], extra={
                "NOYA_VISION_FALLBACK_MODEL": "vision-backup",
            })
        self.assertEqual(result.reason, "http_401")
        self.assertEqual(result.http_status, 401)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any("PRIVATE-TEST-KEY" in msg for msg in logs.output))

    def test_fallback_uses_different_configured_vision_model_once(self):
        original = _response(None)
        fallback = _response("این عکس فال است")
        result, calls = _run([original, fallback], extra={
            "NOYA_VISION_FALLBACK_MODEL": "vision-backup",
        })
        self.assertEqual(result.text, "این عکس فال است")
        self.assertEqual([call["model"] for call in calls],
                         ["vision-primary", "vision-backup"])

    def test_timeout_is_categorized_without_response_body(self):
        with self.assertLogs("botapp.noya_vision_client", "WARNING") as logs:
            result, _ = _run([httpx.ReadTimeout("sensitive signed URL")])
        self.assertEqual(result.reason, "timeout")
        self.assertNotIn("sensitive signed URL", " ".join(logs.output))

    def test_invalid_or_missing_image_rejected_before_provider(self):
        with patch.dict(os.environ, _env()), patch(
            "botapp.noya_vision_client.httpx.AsyncClient"
        ) as transport:
            invalid = async_to_sync(read_vision_frames)(
                [("application/octet-stream", b"arbitrary")], "read",
            )
            self.assertEqual(invalid.reason, "invalid_frames")
            transport.assert_not_called()

    def test_unpack_never_leaks_reasoning_only_provider_content(self):
        value = {"choices": [{
            "message": {"content": None, "reasoning_content": "private thought"},
            "finish_reason": "length",
        }]}
        visible, reason, _ = _unpack_vision_content(value)
        self.assertEqual(visible, "")
        self.assertEqual(reason, "empty_content_length")


@override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
class HistoricalImageDiagnosticsTests(TestCase):
    def test_download_failure_and_provider_failure_are_not_confused(self):
        from botapp.message_archive import archive_message
        from botapp.noya_archive_access import _SCOPE, inspect_group_image
        from botapp.models import MessageSnapshot
        from django.utils import timezone
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=-1001234567890, type="supergroup", username=None, is_forum=False),
            message_id=120, message_thread_id=0, from_user=SimpleNamespace(
                id=200, full_name="member", first_name="member", last_name="", username=None,
            ),
            date=timezone.now(), photo=[SimpleNamespace(
                file_id="actual-file-id", file_size=1234, width=700, height=700,
            )],
            text=None, caption="", reply_to_message=None, sender_chat=None,
            video=None, sticker=None, animation=None, audio=None, voice=None, document=None,
        )
        archive_message(msg, bot_id=222)
        bot = SimpleNamespace(
            get_file=AsyncMock(side_effect=RuntimeError("private token leak")),
        )
        scope = {
            "chat": msg.chat, "chat_id": msg.chat.id, "chat_type": "supergroup",
            "bot": bot, "bot_id": 222, "thread_id": 0, "current_message_id": 130,
            "queries": 0, "vision_calls": 0,
        }
        async def act():
            token = _SCOPE.set(scope)
            try:
                return await inspect_group_image("120")
            finally:
                _SCOPE.reset(token)
        with self.assertLogs("botapp.noya_archive_access", "WARNING") as logs:
            failure = async_to_sync(act)()
        self.assertIn("تلگرام", failure)
        self.assertIn("telegram_download", " ".join(logs.output))
        self.assertNotIn("private token leak", " ".join(logs.output))
        row = MessageSnapshot.objects.get(chat_id=msg.chat.id, message_id=120)
        self.assertNotIn("noya_media", row.metadata)
