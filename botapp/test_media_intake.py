"""Media intake tests: no network, Telegram API, STT provider, or ffmpeg required."""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from PIL import Image

from botapp.media_intake import (
    _attachment, _read_document, _stt_url, is_transcription_request,
    prepare_media_for_noya, transcribe_audio,
)


def _bot(raw=b"hello"):
    async def download(path, destination):
        destination.write(raw)
    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="fake", file_size=len(raw))),
        download_file=AsyncMock(side_effect=download),
    )


def _message(**kwargs):
    fields = dict(voice=None, audio=None, video=None, video_note=None,
                  document=None, photo=None, sticker=None, reply_to_message=None)
    fields.update(kwargs)
    return SimpleNamespace(**fields)


class MediaIntakeTest(SimpleTestCase):
    def test_voice_detected_and_transcribed_from_reply(self):
        voice = SimpleNamespace(file_id="voice-id", file_size=10, mime_type="audio/ogg")
        message = _message(reply_to_message=_message(voice=voice))
        with patch("botapp.media_intake.transcribe_audio", new_callable=AsyncMock, return_value="سلام دنیا") as stt:
            result = async_to_sync(prepare_media_for_noya)(_bot(b"ogg data"), message)
        self.assertTrue(result.has_audio)
        self.assertEqual(result.transcript, "سلام دنیا")
        self.assertIn("UNTRUSTED", result.context)
        self.assertIn("source=reply", result.context)
        stt.assert_awaited_once()

    def test_oversized_media_is_rejected_before_download(self):
        voice = SimpleNamespace(file_id="voice-id", file_size=25_000_000, mime_type="audio/ogg")
        bot = _bot()
        result = async_to_sync(prepare_media_for_noya)(bot, _message(voice=voice))
        self.assertTrue(result.has_audio)
        self.assertFalse(result.transcript)
        self.assertTrue(result.warnings)
        bot.get_file.assert_not_awaited()

    def test_plain_text_file_is_passed_to_llm_as_untrusted_content(self):
        doc = SimpleNamespace(file_id="file-1", file_name="notes.txt",
                              file_size=9, mime_type="text/plain")
        result = async_to_sync(prepare_media_for_noya)(_bot("یادداشت".encode()), _message(document=doc))
        self.assertIn("یادداشت", result.context)
        self.assertIn("[ATTACHED_DOCUMENT", result.context)

    def test_unsupported_binary_is_not_forwarded(self):
        doc = SimpleNamespace(file_id="file-1", file_name="program.exe",
                              file_size=100, mime_type="application/octet-stream")
        bot = _bot(b"MZ")
        result = async_to_sync(prepare_media_for_noya)(bot, _message(document=doc))
        self.assertTrue(result.warnings)
        self.assertEqual(result.context, "")
        bot.get_file.assert_not_awaited()

    def test_video_provides_frame_and_transcript(self):
        video = SimpleNamespace(file_id="v", file_size=9, mime_type="video/mp4")
        img = Image.new("RGB", (8, 8), "blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        async def fake_ffmpeg(raw, *, frame=False):
            return buf.getvalue() if frame else b"mp3_data"
        with patch("botapp.media_intake._ffmpeg", side_effect=fake_ffmpeg), patch(
            "botapp.media_intake.transcribe_audio", new_callable=AsyncMock, return_value="ویدیو"
        ) as stt:
            result = async_to_sync(prepare_media_for_noya)(_bot(b"video"), _message(video=video))
        self.assertEqual(result.transcript, "ویدیو")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0]["source"], "video_frame")
        self.assertEqual(stt.await_args.kwargs["suffix"], ".mp3")

    def test_stt_request_is_multipart_and_customizable(self):
        response = MagicMock()
        response.json.return_value = {"text": "سلام"}
        response.raise_for_status.return_value = None
        client = AsyncMock()
        client.post.return_value = response
        cm = AsyncMock()
        cm.__aenter__.return_value = client
        cm.__aexit__.return_value = None
        with patch.dict("os.environ", {
            "NOYA_STT_API_URL": "https://speech.example/v1/audio/transcriptions",
            "NOYA_STT_MODEL": "whisper-1",
            "NOYA_STT_API_KEY": "test-token",
            "NOYA_STT_ENABLED": "true",
        }), patch("botapp.media_intake.httpx.AsyncClient", return_value=cm):
            text = async_to_sync(transcribe_audio)(b"audio", suffix=".mp3", mime="audio/mpeg")
        self.assertEqual(text, "سلام")
        self.assertEqual(client.post.await_args.kwargs["data"]["model"], "whisper-1")
        self.assertEqual(client.post.await_args.kwargs["files"]["file"][0], "noya_input.mp3")

    def test_agent_loop_forwards_images_to_model(self):
        from botapp.agent.loop import run_agent_loop
        response = MagicMock()
        response.text = '{"choices":[{"message":{"content":"تصویر را دیدم"},"finish_reason":"stop"}]}'
        response.json.return_value = {
            "choices": [{"message": {"content": "تصویر را دیدم"}, "finish_reason": "stop"}]
        }
        response.status_code = 200
        client = AsyncMock()
        client.post.return_value = response
        cm = AsyncMock()
        cm.__aenter__.return_value = client
        with patch.dict("os.environ", {
            "NOYA_API_URL": "http://router/v1/chat/completions",
            "NOYA_MODEL": "text-only",
            "NOYA_VISION_MODEL": "vision-model",
        }), patch("botapp.agent.loop.httpx.AsyncClient", return_value=cm):
            text, metadata = async_to_sync(run_agent_loop)(
                "این عکس چیست؟", "test-session",
                images=[{"mime": "image/jpeg", "data": b"jpeg-bytes"}],
            )
        self.assertEqual(text, "تصویر را دیدم")
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["model"], "vision-model")
        content = payload["messages"][-1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_document_null_bytes_rejected(self):
        with self.assertRaises(ValueError):
            _read_document(b"abc\x00xyz", ".txt")

    def test_transcription_intent(self):
        self.assertTrue(is_transcription_request("این ویس رو تبدیل به متن کن"))
        self.assertFalse(is_transcription_request("یک تصویر بساز"))

    def test_fallback_stt_url(self):
        with patch.dict("os.environ", {
            "NOYA_STT_API_URL": "",
            "NOYA_API_URL": "http://router/v1/chat/completions",
        }):
            self.assertEqual(_stt_url(), "http://router/v1/audio/transcriptions")

    def test_image_document_left_to_vision_pipeline(self):
        doc = SimpleNamespace(file_id="i", file_name="x.jpg", mime_type="image/jpeg")
        self.assertIsNone(_attachment(_message(document=doc)))
