"""Security and archive regressions for NoyaBot's staff-only Telegram inbox."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, User
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from botapp.bot_inbox import archive_message, purge_expired
from botapp.models import BotInboxMessage


def fake_message(chat_id=-10055, message_id=5, text="سلام", *, private=False, sender_id=777):
    return Message(
        message_id=message_id, date=timezone.now(),
        chat=Chat(id=chat_id, type="private" if private else "supergroup", title="گروه نویا"),
        from_user=User(id=sender_id, is_bot=False, first_name="Tester"),
        text=text,
    )


@override_settings(BOT_INBOX_ENABLED=True)
class InboxArchiveTests(TestCase):
    def test_group_and_private_chat_are_persisted_separately_by_bot(self):
        g=fake_message()
        archive_message(g, 111)
        archive_message(g, 222)
        p=fake_message(chat_id=777, private=True, message_id=6, text="پیوی خصوصی")
        archive_message(p, 111)
        self.assertEqual(BotInboxMessage.objects.count(), 3)
        self.assertEqual(BotInboxMessage.objects.filter(bot_id=111).count(), 2)
        self.assertEqual(BotInboxMessage.objects.filter(bot_id=222).count(), 1)

    def test_same_update_is_idempotent_and_edit_is_recorded(self):
        message=fake_message(text="قبل")
        archive_message(message, 111)
        archive_message(message, 111)
        edited=fake_message(text="بعد")
        archive_message(edited, 111)
        self.assertEqual(BotInboxMessage.objects.count(), 1)
        row=BotInboxMessage.objects.get(bot_id=111)
        self.assertEqual(row.text,"بعد")
        self.assertIsNotNone(row.edited_at)

    def test_bots_do_not_archive_without_opt_in(self):
        with override_settings(BOT_INBOX_ENABLED=False):
            self.assertIsNone(archive_message(fake_message(), 333))

    def test_purge_old_uses_retention_window(self):
        archive_message(fake_message(message_id=20),111)
        archive_message(fake_message(message_id=21),111)
        BotInboxMessage.objects.filter(message_id=20).update(
            archived_at=timezone.now()-timedelta(days=32)
        )
        self.assertEqual(purge_expired(days=30),1)
        self.assertTrue(BotInboxMessage.objects.filter(message_id=21).exists())

    def test_outer_middleware_records_not_just_ai_triggers(self):
        from botapp.agent_handlers import ArchiveMiddleware

        async def handler(event, data):
            return "processed"

        with patch("botapp.agent_handlers.activity.record_message_activity"):
            result=async_to_sync(ArchiveMiddleware().__call__)(
                handler,fake_message(chat_id=444,private=True,text="پیام عادی"),
                {"bot": SimpleNamespace(id=111)},
            )
        self.assertEqual(result,"processed")
        self.assertTrue(BotInboxMessage.objects.filter(bot_id=111,chat_id=444).exists())


@override_settings(BOT_INBOX_ENABLED=True)
class InboxApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_cls=get_user_model()
        cls.staff=user_cls.objects.create_user(username="inboxstaff",password="safe-test-password",is_staff=True)
        cls.normal=user_cls.objects.create_user(username="ordinary",password="safe-test-password")
        cls.messages=[
            BotInboxMessage.objects.create(bot_id=111,chat_id=444,message_id=1,chat_title="A",
                                           chat_type="private",sender_name="Alice",text="private for bot 111"),
            BotInboxMessage.objects.create(bot_id=222,chat_id=444,message_id=1,chat_title="B",
                                           chat_type="private",sender_name="Bob",text="secret for bot 222"),
            BotInboxMessage.objects.create(bot_id=111,chat_id=-1001,message_id=2,chat_title="Test Group",
                                           chat_type="supergroup",sender_name="Alice",text="<script>alert(1)</script>"),
        ]

    def test_unauthorized_and_nonstaff_cannot_access(self):
        for path in ("/inbox/","/inbox/api/bots/","/inbox/api/chats/?bot_id=111",
                     "/inbox/api/messages/?bot_id=111&chat_id=444"):
            self.assertEqual(self.client.get(path).status_code,401)
            self.client.force_login(self.normal)
            self.assertEqual(self.client.get(path).status_code,403)
            self.client.logout()

    def test_staff_can_list_known_bots_and_filter_dialogues(self):
        self.client.force_login(self.staff)
        bots=self.client.get("/inbox/api/bots/").json()
        self.assertEqual({r["id"] for r in bots["bots"]},{"111","222"})
        data=self.client.get("/inbox/api/chats/?bot_id=111").json()
        self.assertEqual({c["id"] for c in data["chats"]},{"444","-1001"})
        messages=self.client.get("/inbox/api/messages/?bot_id=111&chat_id=444").json()
        self.assertEqual(len(messages["messages"]),1)
        self.assertEqual(messages["messages"][0]["text"],"private for bot 111")
        self.assertNotIn("secret for bot 222",json.dumps(messages))

    def test_pagination_is_scoped_by_bot(self):
        self.client.force_login(self.staff)
        a=self.client.get("/inbox/api/messages/?bot_id=111&chat_id=444&before=1").json()
        self.assertEqual(a["messages"],[])
        wrong=self.client.get("/inbox/api/messages/?bot_id=222&chat_id=-1001").json()
        self.assertEqual(wrong["messages"],[])

    def test_read_only_default_and_unconfigured_token_mapping(self):
        self.client.force_login(self.staff)
        response=self.client.post(
            "/inbox/api/send/",
            data=json.dumps({"bot_id":111,"chat_id":444,"text":"سلام"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code,403)
        with override_settings(BOT_INBOX_SEND_ENABLED=True):
            response=self.client.post(
                "/inbox/api/send/",
                data=json.dumps({"bot_id":111,"chat_id":444,"text":"سلام"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code,503)

    @override_settings(BOT_INBOX_SEND_ENABLED=True)
    def test_rejects_send_to_unobserved_chat_and_invalid_text(self):
        self.client.force_login(self.staff)
        for body, expected in (
            ({"bot_id":111,"chat_id":555,"text":"hello"},404),
            ({"bot_id":111,"chat_id":444,"text":""},400),
            ({"bot_id":111,"chat_id":444,"text":"x"*4097},400),
        ):
            response=self.client.post(
                "/inbox/api/send/",data=json.dumps(body),content_type="application/json",
            )
            self.assertEqual(response.status_code,expected)

    @override_settings(BOT_INBOX_SEND_ENABLED=True)
    def test_verified_bot_send_captures_telegram_message(self):
        self.client.force_login(self.staff)
        sent=SimpleNamespace(message_id=52,date=timezone.now())
        with (
            patch.dict("os.environ",{"BOT_INBOX_BOT_IDS":"111,222"}),
            patch("botapp.bot_inbox_views.BOT_TOKENS",("111:dummy","222:dummy")),
            patch("botapp.bot_inbox_views.async_to_sync",return_value=lambda *args: sent) as sender,
        ):
            response=self.client.post(
                "/inbox/api/send/",data=json.dumps(
                    {"bot_id":111,"chat_id":444,"text":"از پنل فرستادم"}
                ),content_type="application/json",
            )
        self.assertEqual(response.status_code,201)
        sender.assert_called_once()
        outgoing=BotInboxMessage.objects.get(bot_id=111,chat_id=444,message_id=52)
        self.assertTrue(outgoing.is_outgoing)
        self.assertEqual(outgoing.text,"از پنل فرستادم")

    def test_csrf_is_required_even_for_logged_in_staff(self):
        from django.middleware.csrf import get_token
        browser=Client(enforce_csrf_checks=True)
        browser.force_login(self.staff)
        with override_settings(BOT_INBOX_SEND_ENABLED=True):
            response=browser.post(
                "/inbox/api/send/",data=json.dumps(
                    {"bot_id":111,"chat_id":444,"text":"hello"}
                ),content_type="application/json",
            )
        self.assertEqual(response.status_code,403)

    def test_page_does_not_render_tokens_or_unescaped_chat_text(self):
        self.client.force_login(self.staff)
        page=self.client.get("/inbox/").content.decode()
        self.assertEqual(self.client.get("/inbox/").status_code,200)
        self.assertNotIn("secret for bot 222",page)
        self.assertNotIn("<script>alert(1)</script>",page)
        self.assertNotIn("111:dummy",page)


class LegacyGroupImportTests(TestCase):
    def test_explicit_dry_run_then_verified_group_import(self):
        from io import StringIO
        from django.core.management import call_command
        from botapp.models import MessageSnapshot

        MessageSnapshot.objects.create(
            chat_id=-10033, message_id=51, text="legacy group content",
            sender_name="Member", content_type="text",
        )
        stdout=StringIO()
        call_command("import_legacy_group_inbox", "--bot-id", "111",
                     "--chat-id", "-10033", stdout=stdout)
        self.assertIn("DRY RUN", stdout.getvalue())
        self.assertEqual(BotInboxMessage.objects.count(), 0)

        call_command("import_legacy_group_inbox", "--bot-id", "111",
                     "--chat-id", "-10033", "--apply", stdout=StringIO())
        row=BotInboxMessage.objects.get(bot_id=111,chat_id=-10033)
        self.assertEqual(row.text,"legacy group content")
        self.assertFalse(BotInboxMessage.objects.filter(bot_id=222).exists())

        # Import can be replayed without creating duplicate group messages.
        call_command("import_legacy_group_inbox", "--bot-id", "111",
                     "--chat-id", "-10033", "--apply", stdout=StringIO())
        self.assertEqual(BotInboxMessage.objects.count(), 1)

    def test_legacy_import_rejects_private_chat(self):
        from io import StringIO
        from django.core.management import call_command, CommandError

        with self.assertRaises(CommandError):
            call_command("import_legacy_group_inbox", "--bot-id", "111",
                         "--chat-id", "777", "--apply", stdout=StringIO())
