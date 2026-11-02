"""Telegram animation vision: real GIF frames, TGS validation, and routing tests."""
import gzip
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings
from PIL import Image

from botapp.ai.prompts import build_ai_messages
from botapp.animated_media import sample_animation_frames
from botapp.telegram_media import collect_noya_images, image_bytes_from_sticker
from botapp.tgs_renderer import _bounded_tgs_json, _frame_numbers


def _gif() -> bytes:
    frames = [
        Image.new("RGB", (32, 32), color)
        for color in ("red", "green", "blue")
    ]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=200, loop=0)
    return buf.getvalue()


def _bot(raw: bytes):
    async def download(path, destination):
        destination.write(raw)
    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="test.gif", file_size=len(raw))),
        download_file=AsyncMock(side_effect=download),
    )


def _message(**fields):
    defaults = dict(photo=None, sticker=None, animation=None,
                    document=None, reply_to_message=None)
    defaults.update(fields)
    return SimpleNamespace(**defaults)


class AnimatedVisionTest(SimpleTestCase):
    def test_real_gif_is_three_ordered_distinct_frames(self):
        message = _message(animation=SimpleNamespace(file_id="gif", mime_type="image/gif"))
        frames = async_to_sync(collect_noya_images)(_bot(_gif()), message)
        self.assertEqual(len(frames), 3)
        self.assertEqual([f["frame_index"] for f in frames], [1, 2, 3])
        self.assertTrue(all(f["mime"] == "image/jpeg" for f in frames))
        sampled = []
        for item in frames:
            with Image.open(io.BytesIO(item["data"])) as frame:
                sampled.append(frame.getpixel((16, 16)))
        self.assertGreater(sampled[0][0], sampled[0][1])
        self.assertGreater(sampled[1][1], sampled[1][0])
        self.assertGreater(sampled[2][2], sampled[2][0])

    def test_document_gif_routes_to_vision_instead_of_text(self):
        doc = SimpleNamespace(file_id="gif", mime_type="image/gif", file_name="funny.gif")
        frames = async_to_sync(collect_noya_images)(_bot(_gif()), _message(document=doc))
        self.assertEqual(len(frames), 3)

    def test_reply_animation_frames_are_chronological(self):
        reply = _message(animation=SimpleNamespace(file_id="gif", mime_type="image/gif"))
        frames = async_to_sync(collect_noya_images)(_bot(_gif()), _message(reply_to_message=reply))
        self.assertEqual([x["source"] for x in frames], ["reply"] * 3)

    def test_video_sticker_uses_video_decoder_not_thumbnail(self):
        sticker = SimpleNamespace(file_id="video", is_video=True, is_animated=False, thumbnail=None)
        expected = [("image/jpeg", b"jpeg")]
        with patch("botapp.telegram_media._animated_frames", new_callable=AsyncMock,
                   return_value=expected) as renderer:
            frames = async_to_sync(collect_noya_images)(_bot(b"webm"), _message(sticker=sticker))
        self.assertEqual(frames[0]["data"], b"jpeg")
        self.assertEqual(renderer.await_args.kwargs["kind"], "video")

    def test_tgs_sticker_uses_vector_renderer(self):
        sticker = SimpleNamespace(file_id="tgs", is_video=False, is_animated=True, thumbnail=None)
        expected = [("image/jpeg", b"a"), ("image/jpeg", b"b"), ("image/jpeg", b"c")]
        with patch("botapp.telegram_media._animated_frames", new_callable=AsyncMock,
                   return_value=expected) as renderer:
            frames = async_to_sync(collect_noya_images)(_bot(b"tgs"), _message(sticker=sticker))
        self.assertEqual([x["frame_index"] for x in frames], [1, 2, 3])
        self.assertEqual(renderer.await_args.kwargs["kind"], "tgs")

    def test_animated_sticker_thumbnail_fallback(self):
        sticker = SimpleNamespace(
            file_id="bad-tgs", is_animated=True, is_video=False,
            thumbnail=SimpleNamespace(file_id="thumb"),
        )
        thumb = io.BytesIO()
        Image.new("RGB", (16, 16), "yellow").save(thumb, "PNG")
        bot = _bot(thumb.getvalue())
        with patch("botapp.telegram_media._animated_frames", new_callable=AsyncMock,
                   return_value=[]):
            result = async_to_sync(image_bytes_from_sticker)(bot, sticker)
        self.assertEqual(result[0], "image/jpeg")

    def test_worker_bounded_frame_indices(self):
        self.assertEqual(_frame_numbers(1), [0])
        self.assertEqual(_frame_numbers(3), [0, 1, 2])
        self.assertEqual(_frame_numbers(60), [0, 29, 59])

    def test_tgs_gzip_bomb_rejected_before_native_renderer(self):
        oversized = gzip.compress(b"a" * 1_200_000)
        with self.assertRaises(ValueError):
            _bounded_tgs_json(oversized)

    def test_invalid_animation_kind_rejected(self):
        with self.assertRaises(ValueError):
            async_to_sync(sample_animation_frames)(b"not-a-gif", kind="binary")

    @override_settings(NOYA_SYSTEM_PROMPT_ENABLED=False)
    def test_frame_labels_are_sent_to_direct_vision_model(self):
        frames = async_to_sync(collect_noya_images)(
            _bot(_gif()),
            _message(animation=SimpleNamespace(file_id="gif", mime_type="image/gif")),
        )
        result = build_ai_messages("این GIF چه می‌گوید؟", images=frames)
        content = result[-1]["content"]
        labels = [x["text"] for x in content if x["type"] == "text" and "فریم" in x["text"]]
        self.assertEqual(len(labels), 3)
        self.assertTrue(all(x["type"] == "image_url" for x in content if x["type"] == "image_url"))
