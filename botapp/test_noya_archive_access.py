"""Group-scope archive, Telegram link and historical image regressions.

No Telegram credentials, no real provider, no cross-chat database access.
"""
from __future__ import annotations

import io
import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone

from botapp.message_archive import archive_message, telegram_message_link
from botapp.models import MessageSnapshot, NoyaObservedIdentity
from botapp.noya_archive_access import (
    _SCOPE,
    find_group_nickname,
    get_group_message,
    inspect_group_image,
    search_group_messages,
)
from botapp.agent.registry import registry
from botapp.agent.loop import run_agent_loop


CHAT = -1001234567890
OTHER = -1009988776655


def _chat(chat_id=CHAT, *, username=None, kind="supergroup"):
    return SimpleNamespace(id=chat_id, username=username, type=kind)


def _message(mid=120, *, chat=None, thread=7, photo=False, text="", reply=None):
    chat = chat or _chat()
    user = SimpleNamespace(
        id=42, full_name="حسون", first_name="حسون", last_name="",
        username="hassoon", is_bot=False,
    )
    pics = [SimpleNamespace(file_id="photo_file_120", file_size=2500, width=800, height=800)] if photo else []
    return SimpleNamespace(
        chat=chat, message_id=mid, message_thread_id=thread,
        from_user=user, sender_chat=None, text=text, caption="",
        photo=pics, document=None, video=None, audio=None, voice=None,
        sticker=None, animation=None, reply_to_message=reply,
        date=timezone.now(),
    )


def _scope(chat=None, *, thread=7, bot_id=111, bot=None):
    chat = chat or _chat()
    return {
        "chat": chat,
        "chat_id": chat.id,
        "chat_type": chat.type,
        "bot_id": bot_id,
        "bot": bot,
        "thread_id": thread,
        "current_message_id": 200,
        "reply_to_message_id": 120,
        "queries": 0,
        "vision_calls": 0,
    }


async def _with_scope(scope, operation, *args, **kwargs):
    token = _SCOPE.set(scope)
    try:
        return await operation(*args, **kwargs)
    finally:
        _SCOPE.reset(token)


def _query(scope, fn, *args, **kwargs):
    return async_to_sync(_with_scope)(scope, fn, *args, **kwargs)


class ArchiveLinksAndToolsTests(TestCase):
    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_archive_stores_permalink_and_photo_file_id_only_for_observing_bot(self):
        row = archive_message(_message(photo=True), bot_id=111)
        self.assertEqual(
            row.metadata["telegram_link"],
            "https://t.me/c/1234567890/120",
        )
        self.assertEqual(
            row.metadata["media_by_bot"]["111"],
            {"file_id": "photo_file_120", "kind": "photo"},
        )
        self.assertNotIn("photo_file_120", row.text)
        # The same public group update delivered to a second bot maintains a
        # separate file ID without overwriting first bot's valid reference.
        other_photo = _message(photo=True)
        other_photo.photo[0].file_id = "other_bot_file"
        row2 = archive_message(other_photo, bot_id=222)
        self.assertEqual(row2.pk, row.pk)
        row.refresh_from_db()
        self.assertEqual(set(row.metadata["media_by_bot"]), {"111", "222"})
        self.assertEqual(row.metadata["media_by_bot"]["222"]["file_id"], "other_bot_file")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_search_before_and_read_link_cannot_cross_chats_topics_or_future(self):
        archive_message(_message(120, text="فال پرنده"), bot_id=111)
        archive_message(_message(119, text="قبل‌ترش"), bot_id=111)
        archive_message(_message(120, chat=_chat(OTHER), text="secret"), bot_id=111)
        archive_message(_message(118, thread=8, text="other topic"), bot_id=111)
        scope = _scope()

        results = json.loads(_query(scope, search_group_messages, "", 121, 6))
        self.assertEqual([x["message_id"] for x in results["messages"]], [120, 119])
        self.assertEqual(results["messages"][0]["link"], "https://t.me/c/1234567890/120")

        record = json.loads(_query(scope, get_group_message, "https://t.me/c/1234567890/120"))
        self.assertEqual(record["text"], "فال پرنده")
        self.assertIn("Error", _query(scope, get_group_message, "https://t.me/c/9988776655/120"))
        self.assertIn("پیدا نشد", _query(scope, get_group_message, "118"))
        self.assertIn("پیدا نشد", _query(scope, get_group_message, "201"))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=False)
    def test_disabled_archive_denies_all_tools(self):
        self.assertIn("Error", _query(_scope(), search_group_messages))
        self.assertIn("Error", _query(_scope(), find_group_nickname, "حسون"))
        self.assertIn("Error", _query(_scope(), inspect_group_image, "120"))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_only_group_context_can_access_archive(self):
        self.assertIn("Error", _query(_scope(_chat(42, kind="private")), search_group_messages))
        self.assertIn("Error", async_to_sync(get_group_message)("120"))
        self.assertEqual(telegram_message_link(_chat(42, kind="private"), 120), "")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_deleted_and_expired_messages_unavailable(self):
        gone = archive_message(_message(120, text="deleted"), bot_id=111)
        gone.deleted_by_bot_at = timezone.now()
        gone.save(update_fields=["deleted_by_bot_at"])
        old = archive_message(_message(121, text="old"), bot_id=111)
        MessageSnapshot.objects.filter(pk=old.pk).update(
            archived_at=timezone.now() - timedelta(days=90)
        )
        self.assertIn("پیدا نشد", _query(_scope(), get_group_message, "120"))
        self.assertIn("پیدا نشد", _query(_scope(), get_group_message, "121"))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_nickname_lookup_is_same_group_and_distinguishes_assigned_label(self):
        NoyaObservedIdentity.objects.create(
            chat_id=CHAT, user_id=342, first_name="Ali", last_name="Jafari",
            username="hasoon_real", assigned_nickname="حسون",
            assigned_nickname_by=42,
        )
        NoyaObservedIdentity.objects.create(
            chat_id=OTHER, user_id=8888, username="secret_alias",
            assigned_nickname="حسون",
        )
        payload = json.loads(_query(_scope(), find_group_nickname, "حسون"))
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["matches"][0]["user_id"], 342)
        self.assertEqual(payload["matches"][0]["group_assigned_nickname"], "حسون")
        self.assertEqual(payload["matches"][0]["assigned_by_user_id"], 42)

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_image_reference_must_be_for_correct_bot_and_previously_archived(self):
        archive_message(_message(photo=True), bot_id=111)
        archive_message(_message(119, photo=True), bot_id=0)
        denied = _query(_scope(bot_id=222), inspect_group_image, "120")
        self.assertIn("ثبت نشده", denied)
        self.assertIn("ثبت نشده", _query(_scope(), inspect_group_image, "119"))
        self.assertIn("Error", _query(_scope(), inspect_group_image, "https://t.me/c/9988776655/120"))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_historical_image_read_is_really_fetched_and_vision_model_called(self):
        archive_message(_message(photo=True), bot_id=111)
        bot = SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_size=1200, file_path="photo.jpg")),
        )
        async def write_image(path, destination):
            destination.write(b"fake-image")
        bot.download_file = AsyncMock(side_effect=write_image)
        api_answer = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": "روی تصویر نوشته: فال امروز خوب است"}}]},
        )
        with patch.dict(os.environ, {
            "NOYA_API_URL": "https://vision.test/v1/chat/completions",
            "NOYA_API_KEY": "test-secret",
            "NOYA_VISION_MODEL": "vision-test",
        }), patch(
            "botapp.telegram_media.normalize_image_bytes",
            return_value=("image/jpeg", b"JPEG-normalized"),
        ), patch(
            "botapp.noya_archive_access.httpx.AsyncClient",
        ) as transport:
            transport.return_value.__aenter__.return_value.post = AsyncMock(return_value=api_answer)
            payload = json.loads(_query(_scope(bot=bot), inspect_group_image, "120"))
            sent = transport.return_value.__aenter__.return_value.post.await_args.kwargs["json"]

        self.assertIn("فال امروز", payload["visible_text"])
        self.assertEqual(sent["model"], "vision-test")
        self.assertEqual(sent["messages"][0]["content"][1]["type"], "image_url")
        bot.get_file.assert_awaited_once_with("photo_file_120")
        record = MessageSnapshot.objects.get(chat_id=CHAT, message_id=120)
        self.assertIn("فال امروز", record.metadata["noya_media"]["image_text"])

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_archive_lookup_tools_are_in_chat_catalog_without_exposing_sql(self):
        names = {v["function"]["name"] for v in registry.chat_definitions()}
        for required in (
            "search_group_messages", "get_group_message",
            "find_group_nickname", "inspect_group_image",
        ):
            self.assertIn(required, names)
        self.assertNotIn("execute_sql", names)
        self.assertNotIn("read_server_logs", names)

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_lookup_budget_enforced(self):
        scope = _scope()
        for _ in range(8):
            self.assertIn("messages", _query(scope, search_group_messages))
        self.assertIn("Error", _query(scope, search_group_messages))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_group_record_lists_are_read_only_and_group_scoped(self):
        from botapp.models import ChatDailyActivity, GroupSettings, GroupActionLog
        from botapp.noya_archive_access import read_group_records
        today = timezone.localdate()
        ChatDailyActivity.objects.create(
            chat_id=CHAT, day=today, message_count=15,
            media_count=2, unique_sender_count=5,
        )
        ChatDailyActivity.objects.create(
            chat_id=OTHER, day=today, message_count=999,
            media_count=98, unique_sender_count=300,
        )
        NoyaObservedIdentity.objects.create(
            chat_id=CHAT, user_id=342,
            assigned_nickname="حسون",
        )
        group = GroupSettings.objects.create(chat_id=CHAT, chat_title="test group")
        GroupActionLog.objects.create(
            group=group, admin_user_id=42, admin_name="S",
            action="lock", old_value=False, new_value=True,
        )
        records = json.loads(_query(_scope(), read_group_records, "activity"))
        self.assertEqual(records["records"][0]["messages"], 15)
        self.assertNotIn("999", json.dumps(records))
        aliases = json.loads(_query(_scope(), read_group_records, "nicknames"))
        self.assertEqual(aliases["records"][0]["group_assigned_nickname"], "حسون")
        self.assertIn("Error", _query(_scope(), read_group_records, "moderation_logs"))

        member_bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        denied = _scope(bot=member_bot)
        denied["speaker_user_id"] = 42
        self.assertIn("Error", _query(denied, read_group_records, "moderation_logs"))
        member_bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
        allowed = _scope(bot=member_bot)
        allowed["speaker_user_id"] = 42
        events = json.loads(_query(allowed, read_group_records, "moderation_logs"))
        self.assertEqual(events["records"][0]["action"], "lock")
        member_bot.get_chat_member.assert_awaited()

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_legacy_photo_recovers_from_inbox_of_same_bot(self):
        from botapp.models import BotInboxMessage
        from botapp.noya_archive_access import trace_group_message
        now = timezone.now()
        BotInboxMessage.objects.create(
            bot_id=111, chat_id=CHAT, message_id=120, thread_id=7,
            chat_type="supergroup", sender_user_id=444,
            sender_name="Photo sender", content_type="photo",
            media_file_id="legacy_photo_file_120", message_date=now,
        )
        output = json.loads(_query(_scope(), get_group_message, "120"))
        self.assertEqual(output["message_id"], 120)
        self.assertTrue(output["image_available"])
        recovered = MessageSnapshot.objects.get(chat_id=CHAT, message_id=120)
        self.assertEqual(recovered.metadata["media_by_bot"]["111"]["file_id"],
                         "legacy_photo_file_120")
        self.assertEqual(
            recovered.metadata["telegram_link"], "https://t.me/c/1234567890/120",
        )
        trace = json.loads(_query(_scope(), trace_group_message, "120"))
        self.assertTrue(trace["found"])
        self.assertEqual(trace["source"]["user_id"], 444)
        self.assertIn("ثبت نشده", _query(_scope(bot_id=999), inspect_group_image, "120"))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_existing_old_photo_snapshot_is_enriched_from_bot_inbox(self):
        from botapp.models import BotInboxMessage
        archive_message(_message(120, photo=True), bot_id=0)
        BotInboxMessage.objects.create(
            bot_id=111, chat_id=CHAT, message_id=120, thread_id=7,
            chat_type="supergroup", content_type="photo",
            media_file_id="original_inbox_file", message_date=timezone.now(),
        )
        output = json.loads(_query(_scope(), get_group_message, "120"))
        self.assertTrue(output["image_available"])
        row = MessageSnapshot.objects.get(chat_id=CHAT, message_id=120)
        self.assertEqual(row.metadata["media_by_bot"]["111"]["file_id"],
                         "original_inbox_file")

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_inbox_fallback_cannot_cross_bot_chat_topic_or_retention(self):
        from botapp.models import BotInboxMessage
        now = timezone.now()
        BotInboxMessage.objects.create(
            bot_id=222, chat_id=CHAT, message_id=120, thread_id=7,
            chat_type="supergroup", content_type="photo",
            media_file_id="other_bot_secret", message_date=now,
        )
        BotInboxMessage.objects.create(
            bot_id=111, chat_id=OTHER, message_id=121, thread_id=7,
            chat_type="supergroup", content_type="photo",
            media_file_id="other_group_secret", message_date=now,
        )
        BotInboxMessage.objects.create(
            bot_id=111, chat_id=CHAT, message_id=122, thread_id=8,
            chat_type="supergroup", content_type="photo",
            media_file_id="other_thread_secret", message_date=now,
        )
        BotInboxMessage.objects.create(
            bot_id=111, chat_id=CHAT, message_id=123, thread_id=7,
            chat_type="supergroup", content_type="photo",
            media_file_id="expired", message_date=now - timedelta(days=90),
        )
        for mid in (120, 121, 122, 123):
            self.assertIn("پیدا نشد", _query(_scope(), get_group_message, str(mid)))
        self.assertFalse(MessageSnapshot.objects.filter(chat_id=CHAT).exists())

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_fortune_trace_marks_adjacency_unconfirmed_and_reply_verified(self):
        from botapp.noya_archive_access import trace_group_message
        ask = _message(119, text="فال")
        photo = _message(120, photo=True)
        photo.from_user = SimpleNamespace(
            id=999, full_name="PhotoBot", first_name="PhotoBot", last_name="",
            username="photo_bot", is_bot=True,
        )
        photo.date = ask.date + timedelta(seconds=12)
        archive_message(ask, bot_id=111)
        archive_message(photo, bot_id=111)
        guess = json.loads(_query(_scope(), trace_group_message, "120"))
        self.assertEqual(guess["attribution"], "adjacent_unconfirmed")
        self.assertEqual(guess["possible_requester"]["message_id"], 119)
        self.assertEqual(guess["possible_requester"]["name"], "حسون")
        self.assertIn("not proof", guess["caution"])

        photo.reply_to_message = ask
        archive_message(photo, bot_id=111)
        # An updated reply pointer is now confirmed, not a temporal guess.
        confirmed = json.loads(_query(_scope(), trace_group_message, "120"))
        self.assertEqual(confirmed["attribution"], "verified_telegram_reply")
        self.assertEqual(confirmed["direct_reply"]["message_id"], 119)

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_multiple_fortune_requesters_are_not_arbitrarily_attributed(self):
        from botapp.noya_archive_access import trace_group_message
        first = _message(118, text="فال")
        second = _message(119, text="فال")
        second.from_user.id = 88
        second.from_user.full_name = "Other person"
        photo = _message(120, photo=True)
        photo.from_user.id = 999
        photo.date = first.date + timedelta(seconds=20)
        archive_message(first, bot_id=111)
        archive_message(second, bot_id=111)
        archive_message(photo, bot_id=111)
        trace = json.loads(_query(_scope(), trace_group_message, "120"))
        self.assertEqual(trace["attribution"], "ambiguous_multiple_requesters")
        self.assertIsNone(trace["possible_requester"])

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_verified_archive_reply_stages_real_message_id_and_mention(self):
        from botapp.noya_archive_access import reply_to_group_message, _SCOPE
        from botapp.agent.tools import _CHAT_OUTBOX, pop_chat_outbox
        from botapp.management.commands.runbot import _deliver_noya_queued_messages
        archive_message(_message(119, text="فال"), bot_id=111)

        async def stage():
            t1 = _SCOPE.set(_scope())
            t2 = _CHAT_OUTBOX.set([])
            try:
                result = await reply_to_group_message(
                    "https://t.me/c/1234567890/119",
                    "گرفتاری‌ها می‌گذرند؛ صبوری کن <حتماً>.",
                    True,
                )
                return result, pop_chat_outbox()
            finally:
                _CHAT_OUTBOX.reset(t2)
                _SCOPE.reset(t1)

        result, batch = async_to_sync(stage)()
        self.assertIn("آماده", result)
        self.assertEqual(batch[0]["target_message_id"], 119)
        self.assertEqual(batch[0]["mention_user_id"], 42)
        message = SimpleNamespace(
            message_id=200, message_thread_id=7,
            chat=SimpleNamespace(id=CHAT),
            bot=SimpleNamespace(send_message=AsyncMock(
                return_value=SimpleNamespace(message_id=201))),
        )
        handled = async_to_sync(_deliver_noya_queued_messages)(
            message, {"queued_chat_messages": batch},
        )
        self.assertTrue(handled)
        sent = message.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], CHAT)
        self.assertEqual(sent["reply_parameters"].message_id, 119)
        self.assertFalse(sent["reply_parameters"].allow_sending_without_reply)
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn('tg://user?id=42', sent["text"])
        self.assertIn("&lt;حتماً&gt;", sent["text"])

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_archive_reply_requires_same_chat_real_target(self):
        from botapp.noya_archive_access import reply_to_group_message, _SCOPE
        from botapp.agent.tools import _CHAT_OUTBOX, pop_chat_outbox
        archive_message(_message(119, text="فال"), bot_id=111)

        async def stage():
            t1 = _SCOPE.set(_scope())
            t2 = _CHAT_OUTBOX.set([])
            try:
                invalid = await reply_to_group_message(
                    "https://t.me/c/9988776655/119", "should not post",
                )
                missing = await reply_to_group_message("121", "not archived")
                return invalid, missing, pop_chat_outbox()
            finally:
                _CHAT_OUTBOX.reset(t2)
                _SCOPE.reset(t1)
        invalid, missing, outbox = async_to_sync(stage)()
        self.assertIn("Error", invalid)
        self.assertIn("Error", missing)
        self.assertEqual(outbox, [])

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_backfill_replied_photo_inherits_verified_forum_topic(self):
        original = _message(120, photo=True)
        original.message_thread_id = None
        row = archive_message(original, bot_id=111, thread_id_override=7)
        self.assertEqual(row.thread_id, 7)
        row = archive_message(original, bot_id=111, thread_id_override=7)
        self.assertEqual(row.thread_id, 7)
        found = json.loads(_query(_scope(), get_group_message, "120"))
        self.assertTrue(found["image_available"])

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True, NOYA_HISTORY_ENABLED=True)
    def test_model_tool_call_receives_scoped_archive_in_same_agent_turn(self):
        archive_message(_message(120, text="فال خوشحال‌کننده"), bot_id=111)
        calls = [{
            "id": "call1",
            "type": "function",
            "function": {
                "name": "get_group_message",
                "arguments": json.dumps({"reference": "120"}),
            },
        }]
        class Result:
            status_code = 200
            def __init__(self, msg):
                self.result = {"choices": [{"message": msg, "finish_reason": "stop"}]}
            @property
            def text(self):
                return json.dumps(self.result, ensure_ascii=False)
            def json(self):
                return self.result
            def raise_for_status(self):
                pass
        post = AsyncMock(side_effect=[
            Result({"role": "assistant", "content": "", "tool_calls": calls}),
            Result({"role": "assistant", "content": "متن فال خوشحال‌کننده بود"}),
        ])
        with patch("botapp.agent.loop.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post = post
            answer, meta = async_to_sync(run_agent_loop)(
                question="فال چی گفت؟",
                session_id=f"telegram:{CHAT}",
                system_prompt="از ابزار مجاز استفاده کن",
                telegram_context=_scope(),
            )
        self.assertIn("خوشحال‌کننده", answer)
        self.assertEqual(meta["tools_used"], ["get_group_message"])
        next_request = post.await_args_list[1].kwargs["json"]
        self.assertIn("فال خوشحال‌کننده", json.dumps(next_request, ensure_ascii=False))

