"""Regression tests for the live DIGI ANTI fortune photo (93764).

Main archive topic 93764 was an invalid self-message thread; the same bot's
inbox had topic 0 and the photo file ID. Do not merge genuine forum topics.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone

from botapp.bot_inbox import payload_from_message
from botapp.message_archive import archive_message, scoped_message_thread_id
from botapp.models import BotInboxMessage, MessageSnapshot
from botapp.noya_archive_access import (
    _SCOPE, get_group_message, trace_group_message, inspect_group_image,
)
from botapp.noya_history import archived_context_for_message

CHAT_ID = -1003861069387
BOT_ID = 8880029922
PHOTO_ID = 93764
ASK_ID = 93763


def _message(mid=PHOTO_ID, *, thread=None, forum=False, topic=False, photo=True):
    chat = SimpleNamespace(
        id=CHAT_ID, type="supergroup", username=None, is_forum=forum,
    )
    sender = SimpleNamespace(
        id=123, full_name="DIGI ANTI", first_name="DIGI", last_name="",
        username="other_bot", is_bot=True,
    )
    return SimpleNamespace(
        chat=chat, message_id=mid, message_thread_id=thread,
        is_topic_message=topic, from_user=sender, sender_chat=None,
        text=None, caption="", date=timezone.now(),
        photo=[SimpleNamespace(file_id="known_file_id",
                               file_size=5000, width=640, height=640)] if photo else [],
        reply_to_message=None, video=None, document=None, sticker=None,
        animation=None, audio=None, voice=None,
    )


def _scope(*, forum=False, bot_id=BOT_ID):
    chat = SimpleNamespace(
        id=CHAT_ID, type="supergroup", is_forum=forum, username=None,
    )
    return {
        "bot_id": bot_id, "chat_id": CHAT_ID,
        "chat_type": "supergroup", "chat": chat,
        "thread_id": 0, "current_message_id": 94000,
        "queries": 0, "vision_calls": 0,
    }


async def _use_scope(scope, func, reference):
    token = _SCOPE.set(scope)
    try:
        return await func(reference)
    finally:
        _SCOPE.reset(token)


def _request(scope, func, reference):
    return async_to_sync(_use_scope)(scope, func, reference)


@override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
class NonForumPhotoThreadTests(TestCase):
    def _legacy(self, *, thread=PHOTO_ID, sender_id=123, deleted=False,
                inbox_bot=BOT_ID, inbox_thread=0):
        when = timezone.now()
        row = MessageSnapshot.objects.create(
            chat_id=CHAT_ID, message_id=PHOTO_ID,
            thread_id=thread, sender_user_id=sender_id,
            sender_name="DIGI ANTI", content_type="photo",
            created_at=when,
            deleted_by_bot_at=when if deleted else None,
            metadata={"telegram_link": f"https://t.me/c/3861069387/{PHOTO_ID}"},
        )
        BotInboxMessage.objects.create(
            bot_id=inbox_bot, chat_id=CHAT_ID, message_id=PHOTO_ID,
            chat_type="supergroup", thread_id=inbox_thread,
            sender_user_id=123, sender_name="DIGI ANTI",
            content_type="photo", media_file_id="known_file_id",
            message_date=when,
        )
        return row

    def test_nonforum_photo_self_thread_id_is_not_an_actual_topic(self):
        msg = _message(thread=PHOTO_ID, forum=False, topic=False)
        self.assertEqual(scoped_message_thread_id(msg), 0)
        row = archive_message(msg, bot_id=BOT_ID)
        self.assertEqual(row.thread_id, 0)
        self.assertTrue(row.metadata["media_by_bot"][str(BOT_ID)]["file_id"])
        self.assertEqual(payload_from_message(msg, BOT_ID)["thread_id"], 0)

    def test_real_forum_topic_id_equal_to_its_opening_message_is_preserved(self):
        msg = _message(thread=PHOTO_ID, forum=True, topic=True)
        self.assertEqual(scoped_message_thread_id(msg), PHOTO_ID)
        row = archive_message(msg, bot_id=BOT_ID)
        self.assertEqual(row.thread_id, PHOTO_ID)
        self.assertEqual(payload_from_message(msg, BOT_ID)["thread_id"], PHOTO_ID)

    def test_legacy_photo_recovers_only_from_same_bot_inbox_topic_zero(self):
        row = self._legacy()
        output = json.loads(_request(
            _scope(), get_group_message, f"https://t.me/c/3861069387/{PHOTO_ID}",
        ))
        self.assertEqual(output["message_id"], PHOTO_ID)
        self.assertTrue(output["image_available"])
        row.refresh_from_db()
        self.assertEqual(row.thread_id, 0)
        self.assertEqual(
            row.metadata["media_by_bot"][str(BOT_ID)]["file_id"], "known_file_id",
        )

    def test_recovered_photo_restores_fortune_requester_in_same_group_history(self):
        old = self._legacy()
        timestamp = timezone.now() - timedelta(seconds=9)
        MessageSnapshot.objects.create(
            chat_id=CHAT_ID, message_id=ASK_ID, thread_id=0,
            sender_user_id=881875351, sender_name="Reyhaneh",
            text="فال", content_type="text", created_at=timestamp,
        )
        report = json.loads(_request(
            _scope(), trace_group_message, str(PHOTO_ID),
        ))
        self.assertTrue(report["found"])
        self.assertEqual(report["attribution"], "adjacent_unconfirmed")
        self.assertEqual(report["possible_requester"]["name"], "Reyhaneh")
        self.assertNotEqual(report["attribution"], "verified_telegram_reply")
        old.refresh_from_db()
        self.assertEqual(old.thread_id, 0)

        question = _message(mid=94000, thread=0, photo=False)
        context = archived_context_for_message(question, "ریحانه فال")
        self.assertIn(f"#{PHOTO_ID}", context)
        self.assertIn(f"#{ASK_ID}", context)

    def test_cached_image_text_remains_available_after_repair(self):
        row = self._legacy()
        meta = dict(row.metadata)
        meta["noya_media"] = {"image_text": "صبر و امید و پایان دلخوری"}
        row.metadata = meta
        row.save(update_fields=["metadata"])
        data = json.loads(_request(
            _scope(), inspect_group_image, str(PHOTO_ID),
        ))
        self.assertIn("صبر و امید", data["visible_text"])

    def test_different_chat_bot_or_forum_topic_cannot_be_repaired(self):
        for description, kwargs, forum in (
            ("forum", {}, True),
            ("other bot", {"inbox_bot": 99999}, False),
            ("other topic", {"inbox_thread": 456}, False),
            ("wrong thread ID", {"thread": 456}, False),
            ("wrong sender ID", {"sender_id": 555}, False),
            ("deleted", {"deleted": True}, False),
        ):
            with self.subTest(description=description):
                MessageSnapshot.objects.all().delete()
                BotInboxMessage.objects.all().delete()
                row = self._legacy(**kwargs)
                answer = _request(_scope(forum=forum), get_group_message, str(PHOTO_ID))
                self.assertIn("پیدا نشد", answer)
                row.refresh_from_db()
                self.assertNotEqual(row.thread_id, 0)

    def test_missing_inbox_cannot_bypass_topic_scope(self):
        row = self._legacy()
        BotInboxMessage.objects.all().delete()
        self.assertIn(
            "پیدا نشد", _request(_scope(), get_group_message, str(PHOTO_ID)),
        )
        row.refresh_from_db()
        self.assertEqual(row.thread_id, PHOTO_ID)

    def test_later_observation_of_existing_legacy_photo_corrects_thread(self):
        row = self._legacy()
        msg = _message(thread=PHOTO_ID, forum=False, topic=False)
        updated = archive_message(msg, bot_id=BOT_ID)
        self.assertEqual(updated.pk, row.pk)
        row.refresh_from_db()
        self.assertEqual(row.thread_id, 0)
