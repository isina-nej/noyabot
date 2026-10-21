from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from botapp import noya_context
from botapp.noya_context import (
    build_noya_user_payload,
    recent_chat_lines,
    remember_group_message,
    strip_bot_address,
)


def _msg(
    *,
    text="",
    caption=None,
    chat_id=-1001,
    chat_type="supergroup",
    message_id=1,
    reply=None,
    from_user=None,
):
    return SimpleNamespace(
        text=text or None,
        caption=caption,
        message_id=message_id,
        reply_to_message=reply,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=from_user
        or SimpleNamespace(
            id=10,
            full_name="Ali",
            first_name="Ali",
            username="ali",
        ),
    )


class NoyaContextTest(SimpleTestCase):
    def setUp(self):
        with noya_context._lock:
            noya_context._recent.clear()

    def test_strip_bot_address(self):
        self.assertEqual(strip_bot_address("@noya_bot اینو بخون", "noya_bot"), "اینو بخون")
        self.assertEqual(strip_bot_address("نویا اینو بخون", ""), "اینو بخون")
        self.assertEqual(strip_bot_address("سازنده‌ات کیه نویا؟", ""), "سازنده‌ات کیه؟")

    def test_reply_and_parent_are_attached(self):
        parent = _msg(text="پیام اصلی", message_id=1, from_user=SimpleNamespace(
            id=1, full_name="Sara", first_name="Sara", username="sara"
        ))
        replied = _msg(
            text="ریپلای روی اصلی",
            message_id=2,
            reply=parent,
            from_user=SimpleNamespace(id=2, full_name="Reza", first_name="Reza", username="reza"),
        )
        # Nested reply_to on the reply object (Telegram often includes one level).
        replied.reply_to_message = parent
        current = _msg(text="نویا اینو خلاصه کن", message_id=3, reply=replied)

        payload = build_noya_user_payload(current, "اینو خلاصه کن", include_recent=False)
        self.assertIn("[پیام مورد اشاره — Reza (@reza)]", payload)
        self.assertIn("ریپلای روی اصلی", payload)
        self.assertIn("[پیام ریپلای‌شده — Sara (@sara)]", payload)
        self.assertIn("پیام اصلی", payload)
        self.assertIn('"message_id":2', payload)
        self.assertIn('"reply_to_message_id":1', payload)
        self.assertIn("[درخواست فعلی]\nاینو خلاصه کن", payload)

    def test_recent_context_keeps_chat_and_speaker_identity(self):
        first = _msg(
            text="بهبهان هوا خوبه",
            chat_id=-1001,
            message_id=20,
            from_user=SimpleNamespace(id=11, full_name="Sara", first_name="Sara", username="sara"),
        )
        other_chat = _msg(text="قیمت دلار؟", chat_id=-2002, message_id=21,
                          from_user=SimpleNamespace(id=22, full_name="Reza", first_name="Reza", username="reza"))
        remember_group_message(first)
        remember_group_message(other_chat)
        current = _msg(text="نویا جواب بده", chat_id=-1001, message_id=22,
                       from_user=SimpleNamespace(id=33, full_name="Ali", first_name="Ali", username="ali"))
        payload = build_noya_user_payload(current, "جواب بده")
        self.assertIn('"chat_id":-1001', payload)
        self.assertIn('"user_id":11', payload)
        self.assertIn('"text":"نویا جواب بده"', payload)
        self.assertNotIn("قیمت دلار؟", payload)

    def test_name_only_on_reply_reads_message(self):
        replied = _msg(text="فردا جلسه ساعت چند؟", message_id=5)
        current = _msg(text="نویا", message_id=6, reply=replied)
        payload = build_noya_user_payload(current, "سلام", include_recent=False)
        self.assertIn("فردا جلسه ساعت چند؟", payload)
        self.assertIn("به پیام اشاره‌شده", payload)

    def test_recent_buffer_is_included_for_groups(self):
        older = _msg(text="سلام به همه", message_id=10)
        mid = _msg(text="کی میاد؟", message_id=11)
        remember_group_message(older)
        remember_group_message(mid)
        ask = _msg(text="نویا خلاصه کن", message_id=12)
        payload = build_noya_user_payload(ask, "خلاصه کن", recent_limit=5)
        self.assertIn("[گفتگوی اخیر گروه]", payload)
        self.assertIn("سلام به همه", payload)
        self.assertIn("کی میاد؟", payload)
        self.assertIn("[درخواست فعلی]\nخلاصه کن", payload)
        self.assertEqual(
            recent_chat_lines(-1001, exclude_ids={12}),
            [
                "Ali (@ali): سلام به همه",
                "Ali (@ali): کی میاد؟",
            ],
        )

    def test_private_chat_without_reply_stays_bare(self):
        msg = _msg(text="سلام", chat_type="private", chat_id=42)
        self.assertEqual(build_noya_user_payload(msg, "سلام"), "سلام")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=False)
    def test_no_archive_fallback_when_disabled(self):
        msg = _msg(text="نویا؟", message_id=99)
        payload = build_noya_user_payload(msg, "سلام")
        self.assertEqual(payload, "سلام")
