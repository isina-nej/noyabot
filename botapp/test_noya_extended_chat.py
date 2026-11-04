"""Regression coverage for direct reply actions, sticker bytes, profile consent and silence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from botapp.noya_reply_media import (
    classify_reply_command, selected_reply_text, explicit_image_prompt,
)
from botapp.noya_sticker import image_to_webp_sticker
from botapp.noya_silence import silence_eligible, silent_token
from botapp.noya_profile import (
    _forget_profile, _profile_info, _save_profile, profile_birthdate,
    requests_my_profile_image, save_my_profile, opted_in_profile_context,
)


def message(text="", *, chat_type="supergroup", chat_id=-100, sender_id=55):
    return SimpleNamespace(
        text=text, caption=None, edit_date=None,
        chat=SimpleNamespace(type=chat_type, id=chat_id),
        from_user=SimpleNamespace(id=sender_id, first_name="شادی", username="sh", is_bot=False),
        photo=None, sticker=None, animation=None, document=None,
        voice=None, audio=None, video=None, video_note=None,
        reply_to_message=None,
    )


class DirectMediaRoutingTests(SimpleTestCase):
    def test_reply_commands_are_strict(self):
        for phrase in ("عکس", "عکس پرامپت", "تصویر", "image"):
            self.assertEqual(classify_reply_command(phrase, has_reply=True), "image")
        for phrase in ("ویس", "صدا", "voice"):
            self.assertEqual(classify_reply_command(phrase, has_reply=True), "voice")
        for phrase in ("متن", "متن ویس", "transcribe"):
            self.assertEqual(classify_reply_command(phrase, has_reply=True), "text")
        for phrase in ("استیکر", "استیکر کن", "تبدیل به استیکر"):
            self.assertEqual(classify_reply_command(phrase, has_reply=True), "sticker")
        self.assertIsNone(classify_reply_command("عکس", has_reply=False))
        self.assertIsNone(classify_reply_command("عکس این کار رو کن", has_reply=True))

    def test_reply_prompt_is_selected_verbatim(self):
        m = message()
        m.reply_to_message = SimpleNamespace(
            text="یک گربه   سفید با   دم بلند",
            caption=None, rich_message=None,
        )
        self.assertEqual(selected_reply_text(m), "یک گربه   سفید با   دم بلند")
        self.assertEqual(explicit_image_prompt("عکس یک باغ پر گل", has_reply=False), "یک باغ پر گل")
        self.assertIsNone(explicit_image_prompt("عکس پروفایلم", has_reply=True))

    def test_webp_sticker_is_512_by_512_and_bounded(self):
        import io
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (1500, 1000), "blue").save(buffer, format="JPEG")
        out = image_to_webp_sticker(buffer.getvalue())
        self.assertLessEqual(len(out), 512 * 1024)
        with Image.open(io.BytesIO(out)) as result:
            self.assertEqual(result.format, "WEBP")
            self.assertEqual(result.size, (512, 512))
        with self.assertRaises(ValueError):
            image_to_webp_sticker(b"not an image")


class QuietTurnTests(SimpleTestCase):
    def test_only_low_stakes_group_messages_may_be_silent(self):
        self.assertTrue(silence_eligible(message("نویا سلام")))
        for body in ("نویا چرا جواب نمیدی؟", "نویا این یک دستوره", "نویا کمک",
                     "نویا عکس بساز", "نویا متن ویس رو بده", "/start"):
            self.assertFalse(silence_eligible(message(body)), body)
        self.assertFalse(silence_eligible(message("سلام", chat_type="private", chat_id=55)))
        self.assertTrue(silent_token("[[NOYA_SILENT]]", allowed=True))
        self.assertFalse(silent_token("[[NOYA_SILENT]]", allowed=False))
        self.assertFalse(silent_token("باشه [[NOYA_SILENT]]", allowed=True))

    @patch.dict("os.environ", {"NOYA_SILENCE_ENABLED": "false"})
    def test_silence_kill_switch(self):
        self.assertFalse(silence_eligible(message("نویا سلام")))


class ConsentProfileTests(TestCase):
    def test_birthdate_without_year_and_with_year(self):
        self.assertEqual(profile_birthdate(SimpleNamespace(day=4, month=11, year=None)), "11-04")
        self.assertEqual(profile_birthdate(SimpleNamespace(day=4, month=11, year=2003)), "2003-11-04")
        self.assertEqual(profile_birthdate(SimpleNamespace(day=50, month=11, year=None)), "")

    def test_persistence_requires_opt_in_and_forget_deletes_all(self):
        self.assertEqual(_profile_info(55), {})
        _save_profile(55, first_name="Shadi", username="sh", bio="Hello",
                      birthday="11-04", file_id="telegram-file-id")
        result = _profile_info(55)
        self.assertTrue(result["has_photo"])
        self.assertEqual(result["bio"], "Hello")
        self.assertEqual(result["birthdate"], "11-04")
        _forget_profile(55)
        self.assertEqual(_profile_info(55), {})

    def test_group_cannot_save_or_expose_self_profile(self):
        m = message("profile", chat_type="supergroup")
        m.bot = SimpleNamespace(get_chat=AsyncMock(), get_user_profile_photos=AsyncMock())
        m.reply = AsyncMock()
        self.assertFalse(async_to_sync(save_my_profile)(m))
        self.assertEqual(async_to_sync(opted_in_profile_context)(m), "")
        m.bot.get_chat.assert_not_awaited()

    def test_private_self_profile_save_and_read(self):
        m = message("", chat_type="private", chat_id=55)
        m.reply = AsyncMock()
        m.bot = SimpleNamespace(
            get_chat=AsyncMock(return_value=SimpleNamespace(
                bio="کتاب دوست", birthdate=SimpleNamespace(month=10, day=2, year=None),
            )),
            get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(
                photos=[[SimpleNamespace(file_id="private-photo")]],
            )),
        )
        self.assertTrue(async_to_sync(save_my_profile)(m))
        data = _profile_info(55)
        self.assertEqual(data["bio"], "کتاب دوست")
        self.assertEqual(data["birthdate"], "10-02")
        self.assertTrue(data["has_photo"])
        self.assertIn("کتاب دوست", async_to_sync(opted_in_profile_context)(m))
        self.assertNotIn("private-photo", async_to_sync(opted_in_profile_context)(m))
        self.assertTrue(requests_my_profile_image("عکس پروفایلم رو ببین"))


class TimeoutSettingsTests(SimpleTestCase):
    def test_chat_timeout_is_five_minutes(self):
        from botapp.services import NOYA_API_TIMEOUT
        self.assertEqual(NOYA_API_TIMEOUT, 300.0)
