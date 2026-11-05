"""Noya's archived video note, GIF and sticker reading regressions.

No real Telegram downloads, video decoders, STT providers or Vision calls.
Media bytes are never logged or saved to the archive.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from botapp.message_archive import archive_message
from botapp.models import MessageSnapshot, BotInboxMessage
from botapp.noya_archive_access import _SCOPE, get_group_message
from botapp.noya_archive_visual import inspect_group_media, _sample_media
from botapp.media_intake import prepare_media_for_noya

CHAT = -1009876543210
OTHER = -1001231231230
BOT = 222
MID = 120


def _chat(chat_id=CHAT):
    return SimpleNamespace(id=chat_id, type="supergroup",
                           is_forum=False, username=None)


def _message(kind="sticker", *, mid=MID, chat=None, animated=False, video=False):
    obj = SimpleNamespace(
        file_id="observed-file", file_size=600, is_animated=animated,
        is_video=video, mime_type="image/gif" if kind == "animation" and not video else "video/mp4",
        file_name="motion.gif" if kind == "animation" and not video else "motion.mp4",
    )
    fields = {
        "chat": chat or _chat(), "message_id": mid, "message_thread_id": 0,
        "from_user": SimpleNamespace(
            id=42, full_name="Reyhaneh", first_name="Reyhaneh", last_name="",
            username="reyhaneh", is_bot=False,
        ),
        "date": timezone.now(), "text": None, "caption": "",
        "sender_chat": None, "reply_to_message": None,
        "photo": None, "video_note": None, "video": None, "sticker": None,
        "animation": None, "document": None, "voice": None, "audio": None,
    }
    fields[kind] = obj
    return SimpleNamespace(**fields)


def _bot(raw=b"GIF89a fake data"):
    async def download_file(path, destination):
        destination.write(raw)
    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            file_size=len(raw), file_path="safe-internal-path",
        )),
        download_file=AsyncMock(side_effect=download_file),
    )


def _scope(bot=None, *, bot_id=BOT, chat=None, thread=0):
    chat = chat or _chat()
    return {
        "bot": bot, "bot_id": bot_id, "chat_id": chat.id,
        "chat_type": chat.type, "chat": chat,
        "thread_id": thread, "current_message_id": 200,
        "queries": 0, "vision_calls": 0,
    }


async def _scoped(scope, fn, ref):
    token = _SCOPE.set(scope)
    try:
        return await fn(ref)
    finally:
        _SCOPE.reset(token)


def _read(scope, fn=inspect_group_media, ref="120"):
    return async_to_sync(_scoped)(scope, fn, ref)


@override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
class ArchivedAnimatedMediaTests(TestCase):
    def test_sticker_and_gif_references_recorded_per_bot(self):
        for kind, animated in (("sticker", True), ("animation", False), ("video_note", False)):
            with self.subTest(kind=kind):
                MessageSnapshot.objects.all().delete()
                row = archive_message(_message(kind, animated=animated), bot_id=BOT)
                self.assertEqual(row.content_type, kind)
                ref = row.metadata["media_by_bot"][str(BOT)]
                self.assertEqual(ref["file_id"], "observed-file")
                self.assertEqual(ref["kind"], kind)
                if kind == "sticker":
                    self.assertTrue(ref["is_animated"])
                self.assertNotIn("observed-file", row.text)
                self.assertNotIn("observed-file", row.caption)

    def test_gif_ocr_read_from_three_real_sampled_frames_and_cached(self):
        row = archive_message(_message("animation"), bot_id=BOT)
        bot = _bot()
        frames = [("image/jpeg", b"jpeg-one"), ("image/jpeg", b"jpeg-two"), ("image/jpeg", b"jpeg-three")]
        with patch("botapp.noya_archive_visual._sample_media",
                   new=AsyncMock(return_value=frames)) as sample, patch(
            "botapp.noya_archive_visual._read_visible_text",
            new=AsyncMock(return_value="نوشتهٔ روی گیف: خوش اومدی"),
        ) as vision:
            result = json.loads(_read(_scope(bot=bot)))
        self.assertEqual(result["kind"], "animation")
        self.assertEqual(result["sampled_frames"], 3)
        self.assertIn("خوش اومدی", result["visible_text"])
        self.assertEqual(result["speech_transcript"], "")
        self.assertFalse(result["cached"])
        vision.assert_awaited_once_with(frames, "animation")
        sample.assert_awaited_once()
        bot.get_file.assert_awaited_once_with("observed-file")
        row.refresh_from_db()
        self.assertIn("خوش اومدی", row.metadata["noya_media"]["image_text"])
        with patch("botapp.noya_archive_visual._read_visible_text",
                   new=AsyncMock(side_effect=AssertionError("should use cached OCR"))):
            cache = json.loads(_read(_scope(bot=bot)))
        self.assertTrue(cache["cached"])
        self.assertIn("خوش اومدی", cache["visible_text"])

    def test_video_note_recovers_both_visible_text_and_speech(self):
        row = archive_message(_message("video_note"), bot_id=BOT)
        bot = _bot(b"fake mp4 bytes")
        with patch(
            "botapp.noya_archive_visual._sample_media",
            new=AsyncMock(return_value=[("image/jpeg", b"frame")] * 3),
        ), patch(
            "botapp.noya_archive_visual._voice_transcript",
            new=AsyncMock(return_value=("سلام دوستان، امروز چطورین؟", "")),
        ) as stt, patch(
            "botapp.noya_archive_visual._read_visible_text",
            new=AsyncMock(return_value="روی کاغذ نوشته سلام"),
        ):
            content = json.loads(_read(_scope(bot=bot)))
        self.assertEqual(content["kind"], "video_note")
        self.assertEqual(content["sampled_frames"], 3)
        self.assertIn("سلام دوستان", content["speech_transcript"])
        self.assertIn("روی کاغذ", content["visible_text"])
        stt.assert_awaited_once_with(b"fake mp4 bytes")
        row.refresh_from_db()
        self.assertEqual(row.metadata["noya_media"]["transcript"],
                         "سلام دوستان، امروز چطورین؟")
        self.assertIn("روی کاغذ", row.metadata["noya_media"]["image_text"])

    def test_silent_video_note_never_invents_audio(self):
        archive_message(_message("video_note"), bot_id=BOT)
        with patch("botapp.noya_archive_visual._sample_media",
                   new=AsyncMock(return_value=[("image/jpeg", b"sample")])), patch(
            "botapp.noya_archive_visual._voice_transcript",
            new=AsyncMock(return_value=("", "صدای قابل تبدیل نبود.")),
        ), patch(
            "botapp.noya_archive_visual._read_visible_text",
            new=AsyncMock(return_value="فقط یک لبخند دیده می‌شود."),
        ):
            out = json.loads(_read(_scope(bot=_bot())))
        self.assertEqual(out["speech_transcript"], "")
        self.assertIn("صدای قابل تبدیل نبود", out["audio_warning"])
        self.assertIn("لبخند", out["visible_text"])

    def test_same_bot_legacy_inbox_recovers_old_gif_or_sticker(self):
        for kind in ("animation", "sticker", "video_note"):
            with self.subTest(kind=kind):
                MessageSnapshot.objects.all().delete()
                BotInboxMessage.objects.all().delete()
                BotInboxMessage.objects.create(
                    bot_id=BOT, chat_id=CHAT, message_id=MID, thread_id=0,
                    chat_type="supergroup", content_type=kind,
                    media_file_id="legacy-reference", message_date=timezone.now(),
                )
                data = json.loads(_read(_scope(), get_group_message))
                self.assertTrue(data["media_available"])
                self.assertEqual(data["media_kind"], kind)
                row = MessageSnapshot.objects.get(chat_id=CHAT, message_id=MID)
                self.assertEqual(
                    row.metadata["media_by_bot"][str(BOT)]["file_id"],
                    "legacy-reference",
                )

    def test_cannot_read_other_bot_group_or_topic_even_with_cached_ocr(self):
        row = archive_message(_message("sticker"), bot_id=BOT)
        meta = dict(row.metadata)
        meta["noya_media"] = {"image_text": "confidential group text"}
        row.metadata = meta
        row.save(update_fields=["metadata"])
        denied = _read(_scope(bot=_bot(), bot_id=333))
        self.assertIn("ثبت نشده", denied)
        self.assertNotIn("confidential group text", denied)
        self.assertIn("Error", _read(_scope(bot=_bot()), ref="https://t.me/c/1231231230/120"))
        self.assertIn("ثبت نشده", _read(_scope(bot=_bot(), thread=88)))

    def test_oversize_remote_file_not_downloaded(self):
        archive_message(_message("animation"), bot_id=BOT)
        bot = _bot()
        bot.get_file.return_value.file_size = 35_000_000
        self.assertIn("حجم", _read(_scope(bot=bot)))
        bot.download_file.assert_not_awaited()

    def test_vision_not_configured_must_not_claim_text(self):
        archive_message(_message("sticker"), bot_id=BOT)
        with patch("botapp.noya_archive_visual._sample_media",
                   new=AsyncMock(return_value=[("image/jpeg", b"sample")])), patch(
            "botapp.noya_archive_visual._read_visible_text",
            new=AsyncMock(return_value=""),
        ):
            response = json.loads(_read(_scope(bot=_bot())))
        self.assertEqual(response["visible_text"], "")
        self.assertIn("Vision", response["vision_warning"])

    def test_sample_media_uses_correct_decoder_for_tgs_and_video_gif(self):
        from botapp.animated_media import sample_animation_frames
        mock_frame = [("image/jpeg", b"jpg")]
        with patch("botapp.animated_media.sample_animation_frames",
                   new=AsyncMock(return_value=mock_frame)) as renderer:
            tgs = async_to_sync(_sample_media)(
                b"\x1f\x8bcompressed", "sticker", {"kind": "sticker"},
            )
            self.assertEqual(tgs, mock_frame)
            self.assertEqual(renderer.await_args.kwargs["kind"], "tgs")
            mp4 = async_to_sync(_sample_media)(
                b"\x00\x00\x00\x18ftypisom", "animation", {"kind": "animation"},
            )
            self.assertEqual(mp4, mock_frame)
            self.assertEqual(renderer.await_args.kwargs["kind"], "video")
            gif = async_to_sync(_sample_media)(
                b"GIF89a fake", "animation", {"kind": "animation"},
            )
            self.assertEqual(gif, mock_frame)
            self.assertEqual(renderer.await_args.kwargs["kind"], "image")

    def test_live_video_note_uses_three_frames_and_stt(self):
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(image, format="JPEG")
        sample_frames = [("image/jpeg", image.getvalue()) for _ in range(3)]
        message = _message("video_note")
        with patch("botapp.animated_media.sample_animation_frames",
                   new=AsyncMock(return_value=sample_frames)) as frames, patch(
            "botapp.media_intake._ffmpeg",
            new=AsyncMock(return_value=b"audio-mp3"),
        ), patch(
            "botapp.media_intake.transcribe_audio",
            new=AsyncMock(return_value="ویدیو‌مسج میگه سلام"),
        ) as stt:
            prepared = async_to_sync(prepare_media_for_noya)(
                _bot(b"video-note"), message, include_reply=False,
            )
        self.assertEqual(len(prepared.images), 3)
        self.assertEqual([x["frame_index"] for x in prepared.images], [1, 2, 3])
        self.assertIn("سلام", prepared.transcript)
        frames.assert_awaited_once()
        stt.assert_awaited_once()
