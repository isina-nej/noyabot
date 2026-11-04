"""History, topic isolation, consent and nickname regression coverage."""
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from botapp.models import MessageSnapshot, NoyaObservedIdentity
from botapp.noya_history import archived_context_for_message
from botapp.noya_identity import (
    _nickname_from_text, clear_nickname, nickname_context_for_message,
    observe_identity,
)
from botapp.noya_archive_media import save_prepared_media


def _message(*, chat_id=-1001, thread_id=7, message_id=250, text="",
             first_name="سارا", last_name="احمدی", username="sara"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        message_thread_id=thread_id,
        message_id=message_id,
        text=text,
        from_user=SimpleNamespace(
            id=33, first_name=first_name, last_name=last_name,
            full_name=(first_name + " " + last_name).strip(),
            username=username, is_bot=False,
        ),
    )


@override_settings(
    MESSAGE_ARCHIVE_ENABLED=True, MESSAGE_ARCHIVE_RETENTION_DAYS=30,
    NOYA_HISTORY_ENABLED=True, NOYA_HISTORY_SCAN_MESSAGES=200,
    NOYA_HISTORY_SELECTED_LINES=30, NOYA_HISTORY_MAX_CHARS=8000,
)
class NoyaHistoryTests(TestCase):
    def setUp(self):
        MessageSnapshot.objects.bulk_create([
            MessageSnapshot(
                chat_id=-1001, thread_id=7, message_id=n,
                sender_user_id=33, sender_name="سارا احمدی",
                sender_username="sara",
                text=("آزمایش قاصدک از فردا" if n == 21 else f"متن گفتگو {n}"),
            ) for n in range(1, 201)
        ])
        NoyaObservedIdentity.objects.create(
            chat_id=-1001, user_id=33, username="sara",
            first_name="سارا", last_name="احمدی", nickname="قاصدک",
        )

    def test_scans_200_but_selects_relevant_subset(self):
        body = archived_context_for_message(_message(), "قاصدک آزمایش")
        self.assertIn("آزمایش قاصدک", body)
        self.assertIn("لقب اعلام‌شده: قاصدک", body)
        self.assertIn("GROUP_HISTORY_UNTRUSTED", body)
        self.assertLessEqual(body.count("user_id="), 30)
        self.assertLess(len(body), 12000)

    def test_never_crosses_chat_topic_or_future(self):
        for chat_id, thread_id, mid, marker in [
            (-1002, 7, 180, "SECRET_OTHER_CHAT"),
            (-1001, 9, 181, "SECRET_OTHER_TOPIC"),
            (-1001, 7, 251, "SECRET_FUTURE"),
        ]:
            MessageSnapshot.objects.create(
                chat_id=chat_id, thread_id=thread_id, message_id=mid,
                text=marker,
            )
        body = archived_context_for_message(_message(), "SECRET")
        self.assertNotIn("SECRET_OTHER_CHAT", body)
        self.assertNotIn("SECRET_OTHER_TOPIC", body)
        self.assertNotIn("SECRET_FUTURE", body)

    def test_deleted_by_bot_message_is_not_available(self):
        from django.utils import timezone
        MessageSnapshot.objects.filter(chat_id=-1001, message_id=21).update(
            deleted_by_bot_at=timezone.now(),
        )
        body = archived_context_for_message(_message(), "آزمایش")
        self.assertNotIn("آزمایش قاصدک", body)

    @override_settings(MESSAGE_ARCHIVE_ENABLED=False)
    def test_history_is_opt_in(self):
        self.assertEqual(archived_context_for_message(_message(), "آزمایش"), "")

    def test_voice_and_video_enrichment_appears_without_raw_files(self):
        snapshot = MessageSnapshot.objects.get(chat_id=-1001, message_id=200)
        snapshot.content_type = "video"
        snapshot.metadata = {"noya_media": {
            "transcript": "جلسه فردا ساعت ده است",
            "visual_summary": "یک تخته و جدول روی آن دیده می‌شود",
        }}
        snapshot.save(update_fields=["content_type", "metadata"])
        body = archived_context_for_message(_message(), "جلسه فردا")
        self.assertIn("جلسه فردا ساعت ده", body)
        self.assertIn("تخته و جدول", body)


class NoyaIdentityTests(TestCase):
    def test_only_self_declared_nickname(self):
        self.assertEqual(_nickname_from_text("منو قاصدک صدا کن"), "قاصدک")
        self.assertEqual(_nickname_from_text("اسم علی قاصدک است"), "")
        self.assertEqual(_nickname_from_text("/nickname قاصدک"), "قاصدک")
        self.assertEqual(_nickname_from_text("/nickname https://evil.com"), "")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True)
    def test_public_name_and_alias_are_stored_per_chat(self):
        message = _message(text="منو قاصدک صدا کن")
        self.assertTrue(observe_identity(message))
        row = NoyaObservedIdentity.objects.get(chat_id=-1001, user_id=33)
        self.assertEqual((row.first_name, row.last_name, row.nickname), ("سارا", "احمدی", "قاصدک"))
        self.assertIn("self_chosen_nickname", nickname_context_for_message(message))
        self.assertFalse(NoyaObservedIdentity.objects.filter(chat_id=-1002).exists())
        self.assertTrue(clear_nickname(-1001, 33))
        row.refresh_from_db()
        self.assertEqual(row.nickname, "")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=False)
    def test_no_silent_group_profile_collection(self):
        self.assertFalse(observe_identity(_message(text="سلام")))
        self.assertFalse(NoyaObservedIdentity.objects.exists())

    @override_settings(MESSAGE_ARCHIVE_ENABLED=False)
    def test_explicit_alias_allowed_without_full_archive(self):
        self.assertTrue(observe_identity(_message(text="/nickname دوست من")))
        self.assertEqual(NoyaObservedIdentity.objects.get().nickname, "دوست من")


@override_settings(MESSAGE_ARCHIVE_ENABLED=True)
class NoyaArchivedMediaTests(TestCase):
    def test_saves_existing_transcript_without_storing_file(self):
        MessageSnapshot.objects.create(
            chat_id=-1001, thread_id=7, message_id=250, content_type="voice",
        )
        m = _message()
        m.voice = SimpleNamespace(file_id="private-file-id")
        m.audio = m.video = m.video_note = m.document = None
        prepared = SimpleNamespace(transcript="جلسه ساعت پنج")
        async_to_sync(save_prepared_media)(m, prepared)
        row = MessageSnapshot.objects.get(chat_id=-1001, message_id=250)
        self.assertEqual(row.metadata["noya_media"]["transcript"], "جلسه ساعت پنج")
        self.assertNotIn("private-file-id", str(row.metadata))

    def test_does_not_attribute_reply_audio_to_new_message(self):
        MessageSnapshot.objects.create(
            chat_id=-1001, thread_id=7, message_id=250, content_type="text",
        )
        m = _message()
        m.voice = m.audio = m.video = m.video_note = m.document = None
        m.reply_to_message = SimpleNamespace(voice=SimpleNamespace(file_id="other"))
        async_to_sync(save_prepared_media)(m, SimpleNamespace(transcript="reply speech"))
        self.assertEqual(
            MessageSnapshot.objects.get(chat_id=-1001, message_id=250).metadata, {},
        )
