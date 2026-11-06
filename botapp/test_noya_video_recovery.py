"""Regression for non-seekable Telegram video and malformed 9Router replies.

Exercises a real ffmpeg MP4 with metadata at the end of the file; all
network calls are mocked and no user/media contents are logged.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from PIL import Image

from botapp.agent.loop import run_agent_loop
from botapp.animated_media import (
    MediaDecodeError, _communicate, sample_animation_frames,
)
from botapp.media_intake import prepare_media_for_noya


def _api_response(data=None, *, text=None):
    if text is None:
        text = json.dumps(data, ensure_ascii=False)
    def body():
        if data is None:
            raise ValueError("invalid JSON body")
        return data
    return SimpleNamespace(
        text=text, json=body, status_code=200,
        raise_for_status=lambda: None,
    )


def _run_with_responses(*replies):
    with patch.dict(os.environ, {
        "NOYA_API_URL": "https://router.example/chat/completions",
        "NOYA_API_KEY": "test-credential",
        "NOYA_MODEL": "noya-chat",
        "NOYA_TOOL_MODEL": "noya-chat",
        "NOYA_CHAT_FALLBACK_MODEL": "",
    }), patch(
        "botapp.agent.loop.httpx.AsyncClient.post",
        new=AsyncMock(side_effect=replies),
    ) as post:
        reply, metadata = async_to_sync(run_agent_loop)(
            question="شیشه اسنیف میشه؟", session_id="telegram:-100123",
            system_prompt="Helpful; do not expose reasoning.",
        )
        return reply, metadata, post.await_args_list


class InvalidProviderRecoveryTests(SimpleTestCase):
    def test_invalid_json_recovers_with_single_tool_free_model_call(self):
        invalid = _api_response(text="not json, not SSE")
        valid = _api_response({"choices": [{
            "message": {"role": "assistant", "content": "پاسخ توضیحی و محتاطانه"},
            "finish_reason": "stop",
        }]})
        reply, meta, calls = _run_with_responses(invalid, valid)
        self.assertEqual(reply, "پاسخ توضیحی و محتاطانه")
        self.assertEqual(len(calls), 2)
        self.assertIn("tools", calls[0].kwargs["json"])
        self.assertNotIn("tools", calls[1].kwargs["json"])
        self.assertNotIn("tool_choice", calls[1].kwargs["json"])
        self.assertEqual(calls[1].kwargs["json"]["messages"], calls[0].kwargs["json"]["messages"])

    def test_json_without_choices_gets_one_bounded_retry(self):
        invalid = _api_response({"error": {"message": "provider unavailable"}})
        valid = _api_response({"choices": [{"message": {
            "content": "دلیل رنگ آبی در آن سریال یک انتخاب داستانی است.",
        }}]})
        reply, meta, calls = _run_with_responses(invalid, valid)
        self.assertIn("داستانی", reply)
        self.assertEqual(len(calls), 2)

    def test_invalid_message_gets_one_retry_with_configured_model(self):
        invalid = _api_response({"choices": [{"message": None}]})
        valid = _api_response({"choices": [{"message": {"content": "پاسخ جایگزین"}}]})
        with patch.dict(os.environ, {"NOYA_CHAT_FALLBACK_MODEL": "alternative"}):
            reply, meta, calls = _run_with_responses(invalid, valid)
        self.assertEqual(reply, "پاسخ جایگزین")
        self.assertEqual(len(calls), 2)

    def test_does_not_expose_reasoning_only_retry(self):
        invalid = _api_response({"choices": []})
        reasoning_only = _api_response({"choices": [{
            "message": {"content": None, "reasoning_content": "SECRET internal thought"},
            "finish_reason": "stop",
        }]})
        reply, meta, calls = _run_with_responses(invalid, reasoning_only)
        self.assertEqual(reply, "خطا در پردازش پاسخ هوش مصنوعی.")
        self.assertNotIn("SECRET", reply)
        self.assertEqual(len(calls), 2)

    def test_second_invalid_result_does_not_loop_forever(self):
        invalid = _api_response({"choices": []})
        reply, _meta, calls = _run_with_responses(invalid, invalid)
        self.assertIn("خطا در پردازش", reply)
        self.assertEqual(len(calls), 2)

    def test_tool_free_recovery_never_exposes_tool_calls(self):
        invalid = _api_response({"choices": []})
        bad_retry = _api_response({"choices": [{"message": {
            "content": "should not deliver", "tool_calls": [
                {"id": "x", "function": {"name": "delete_messages", "arguments": "{}"}}
            ],
        }}]})
        reply, meta, calls = _run_with_responses(invalid, bad_retry)
        self.assertIn("خطا در پردازش", reply)
        self.assertEqual(len(calls), 2)


def _demo_mp4():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "demo.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-y", "-f", "lavfi", "-i", "testsrc2=size=96x96:rate=3:duration=4",
                "-c:v", "mpeg4", "-q:v", "8", str(path),
            ], check=True, timeout=15, capture_output=True,
        )
        return path.read_bytes()


class SeekableVideoFallbackTests(SimpleTestCase):
    def test_real_seekable_memfd_video_read_after_pipe_failure(self):
        if sys.platform != "linux" or not hasattr(os, "memfd_create") or not shutil.which("ffmpeg"):
            self.skipTest("Linux memfd and ffmpeg are required")
        raw = _demo_mp4()
        self.assertGreater(raw.find(b"moov"), raw.find(b"mdat"))
        original = _communicate
        async def simulate_stream_error(argv, data, *, timeout, seekable=False):
            if not seekable:
                raise MediaDecodeError("ffmpeg_exit")
            return await original(argv, data, timeout=timeout, seekable=True)
        with patch(
            "botapp.animated_media._communicate",
            new=AsyncMock(side_effect=simulate_stream_error),
        ) as subprocess_call:
            with self.assertLogs("botapp.animated_media", level="WARNING") as logs:
                frames = async_to_sync(sample_animation_frames)(raw, kind="video")
        self.assertEqual(len(frames), 3)
        self.assertEqual(len({item[1] for item in frames}), 3)
        self.assertEqual(subprocess_call.await_count, 2)
        self.assertTrue(subprocess_call.await_args.kwargs["seekable"])
        self.assertTrue(any("stream_failed" in log for log in logs.output))
        for mime, data in frames:
            self.assertEqual(mime, "image/jpeg")
            with Image.open(io.BytesIO(data)) as frame:
                self.assertGreater(frame.width, 0)

    def test_decode_failures_expose_codes_without_filenames_or_bytes(self):
        with patch("botapp.animated_media._communicate",
                   new=AsyncMock(side_effect=MediaDecodeError("ffmpeg_exit"))) as subprocess_call:
            with self.assertLogs("botapp.animated_media", level="WARNING") as logs:
                with self.assertRaises(MediaDecodeError) as caught:
                    async_to_sync(sample_animation_frames)(b"malformed mp4", kind="video")
        self.assertEqual(caught.exception.code, "video_decode_failed")
        self.assertEqual(subprocess_call.await_count, 2)
        self.assertTrue(any("seekable_failed" in line for line in logs.output))
        self.assertFalse(any("malformed mp4" in line for line in logs.output))

    def test_timed_out_decoder_is_not_retried(self):
        with patch("botapp.animated_media._communicate",
                   new=AsyncMock(side_effect=MediaDecodeError("timeout"))) as subprocess_call:
            with self.assertRaises(MediaDecodeError) as caught:
                async_to_sync(sample_animation_frames)(b"mp4", kind="video")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(subprocess_call.await_count, 1)

    def test_circular_video_note_rejects_impossible_stt_length(self):
        jpg = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(jpg, "JPEG")
        obj = SimpleNamespace(file_id="vid", file_size=4000, duration=4)
        msg = SimpleNamespace(
            video_note=obj, video=None, document=None, voice=None,
            audio=None, photo=None, sticker=None, animation=None,
            reply_to_message=None,
        )
        bot = SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(
                file_path="filename.mp4", file_size=4000,
            )),
            download_file=AsyncMock(side_effect=lambda p, destination:
                                    destination.write(b"dummy MP4")),
        )
        with patch(
            "botapp.animated_media.sample_animation_frames",
            new=AsyncMock(return_value=[("image/jpeg", jpg.getvalue())]),
        ), patch(
            "botapp.media_intake._ffmpeg", new=AsyncMock(return_value=b"audio"),
        ), patch(
            "botapp.media_intake.transcribe_audio",
            new=AsyncMock(return_value="speech " * 150),
        ):
            prepared = async_to_sync(prepare_media_for_noya)(bot, msg, include_reply=False)
        self.assertEqual(len(prepared.images), 1)
        self.assertEqual(prepared.transcript, "")
        self.assertTrue(any("سازگار نیست" in warning for warning in prepared.warnings))
