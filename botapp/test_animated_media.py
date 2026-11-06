"""Telegram animation vision: real GIF frames, TGS validation, and routing tests."""
import gzip
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
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

    def test_real_tgs_vector_is_rendered_to_jpeg_frames(self):
        """Exercise the native renderer in a bounded child, not only a mock."""
        simple = {
            "v": "5.5.7", "fr": 30, "ip": 0, "op": 30,
            "w": 64, "h": 64, "nm": "test", "ddd": 0, "assets": [],
            "layers": [{
                "ddd": 0, "ind": 1, "ty": 4, "nm": "rectangle", "sr": 1,
                "ks": {
                    "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [32, 32, 0]}, "a": {"a": 0, "k": [0, 0, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
                "shapes": [
                    {"ty": "gr", "it": [
                        {"ty": "rc", "s": {"a": 0, "k": [32, 32]},
                         "p": {"a": 0, "k": [0, 0]}, "r": {"a": 0, "k": 0}},
                        {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]},
                         "o": {"a": 0, "k": 100}, "r": 1},
                        {"ty": "tr", "p": {"a": 0, "k": [0, 0]},
                         "a": {"a": 0, "k": [0, 0]},
                         "s": {"a": 0, "k": [100, 100]},
                         "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}},
                    ]}
                ],
                "ip": 0, "op": 30, "st": 0, "bm": 0,
            }],
        }
        raw = gzip.compress(json.dumps(simple).encode())
        frames = async_to_sync(sample_animation_frames)(raw, kind="tgs")
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(blob.startswith(b"\xff\xd8") for _, blob in frames))

    def test_tgs_gzip_bomb_rejected_before_native_renderer(self):
        oversized = gzip.compress(b"a" * 1_200_000)
        with self.assertRaises(ValueError):
            _bounded_tgs_json(oversized)

    def test_jpeg_stream_frame_parser_rejects_truncated_data(self):
        from botapp.animated_media import _mjpeg_frames
        a = io.BytesIO()
        b = io.BytesIO()
        Image.new("RGB", (12, 12), "red").save(a, format="JPEG")
        Image.new("RGB", (12, 12), "blue").save(b, format="JPEG")
        samples = _mjpeg_frames(a.getvalue() + b.getvalue())
        self.assertEqual(len(samples), 2)
        self.assertTrue(all(x.startswith(b"\xff\xd8") for x in samples))
        with self.assertRaises(RuntimeError):
            _mjpeg_frames(a.getvalue() + b.getvalue()[:-2])

    def test_video_path_decodes_three_real_jpegs_without_apng(self):
        from botapp.animated_media import _video_to_frames
        first = io.BytesIO()
        middle = io.BytesIO()
        last = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(first, format="JPEG")
        Image.new("RGB", (20, 20), "green").save(middle, format="JPEG")
        Image.new("RGB", (20, 20), "blue").save(last, format="JPEG")
        stream = first.getvalue() + middle.getvalue() + last.getvalue()
        with patch("botapp.animated_media._communicate",
                   new=AsyncMock(return_value=stream)) as worker:
            frames = async_to_sync(_video_to_frames)(b"MP4",)
        self.assertEqual(len(frames), 3)
        self.assertEqual(len({x[1] for x in frames}), 3)
        argv = worker.await_args.args[0]
        self.assertIn("mjpeg", argv)
        self.assertIn("image2pipe", argv)
        self.assertNotIn("apng", argv)

    def test_real_mp4_gif_and_video_note_have_distinct_frames(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is unavailable on this runner")
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=96x96:rate=3:duration=4",
                    "-c:v", "mpeg4", "-q:v", "8",
                    "-movflags", "+faststart", str(filename),
                ],
                check=True, timeout=15, capture_output=True,
            )
            raw = filename.read_bytes()
        for label in ("gif-as-mp4", "video-note"):
            with self.subTest(label=label):
                frames = async_to_sync(sample_animation_frames)(raw, kind="video")
                self.assertEqual(len(frames), 3)
                self.assertEqual(len({data for _, data in frames}), 3)
                for mime, blob in frames:
                    self.assertEqual(mime, "image/jpeg")
                    with Image.open(io.BytesIO(blob)) as img:
                        self.assertGreater(img.width, 0)
                        self.assertLessEqual(max(img.size), 512)

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
