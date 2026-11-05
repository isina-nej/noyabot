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

