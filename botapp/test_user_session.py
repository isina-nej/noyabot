"""Unit tests: Telethon user session must never reply without explicit opt-in."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from botapp.user_session import (
    NoyaUserSession, UserSessionConfig, _envelope, parse_chat_ids,
    should_answer, strip_address,
)


def make_config(*, private=False):
    return UserSessionConfig(
        api_id=12345, api_hash="fake", session="not-used",
        allowed_chat_ids=frozenset({-100777, 222}),
        private_replies=private,
    )


class UserSessionGateTests(SimpleTestCase):
    def test_chat_allowlist_and_self_filter(self):
        args = dict(
            chat_id=-100777, sender_id=111, self_id=999,
            is_private=False, is_group=True, text="نویا سلام",
            reply_sender_id=None, username="myuser", config=make_config(),
        )
        self.assertTrue(should_answer(**args))
        self.assertFalse(should_answer(**{**args, "chat_id": -100888}))
        self.assertFalse(should_answer(**{**args, "sender_id": 999}))
        self.assertFalse(should_answer(**{**args, "sender_id": None}))

    def test_group_trigger_and_mentions(self):
        base = dict(
            chat_id=-100777, sender_id=111, self_id=999,
            is_private=False, is_group=True, reply_sender_id=None,
            username="myuser", config=make_config(),
        )
        self.assertFalse(should_answer(**{**base, "text": "سلام به همه"}))
        self.assertTrue(should_answer(**{**base, "text": "سلام @MyUser جواب بده"}))
        self.assertTrue(should_answer(**{**base, "text": "نویا کمک"}))
        self.assertTrue(should_answer(**{**base, "text": "hi", "reply_sender_id": 999}))
        self.assertFalse(should_answer(**{**base, "text": "hi", "reply_sender_id": 101}))
        self.assertFalse(should_answer(**{**base, "text": "hi", "is_group": False}))

    def test_private_chats_are_off_even_when_allowlisted(self):
        args = dict(
            chat_id=222, sender_id=222, self_id=999, is_private=True,
            is_group=False, text="hello", reply_sender_id=None, username="me",
        )
        self.assertFalse(should_answer(**args, config=make_config(private=False)))
        self.assertTrue(should_answer(**args, config=make_config(private=True)))

    def test_channel_broadcast_not_a_trigger(self):
        self.assertFalse(should_answer(
            chat_id=-100777, sender_id=111, self_id=999,
            is_private=False, is_group=False, text="نویا",
            reply_sender_id=999, username="me", config=make_config(),
        ))

    def test_address_stripping(self):
        self.assertEqual(strip_address("نویا، @myuser سلام", "myuser"), "سلام")
        self.assertEqual(strip_address("Noya help", ""), "help")
        self.assertEqual(strip_address("@myuser", "myuser"), "سلام")

    def test_chat_id_parser(self):
        self.assertEqual(parse_chat_ids(" -100777,222  ; -100777"), frozenset({-100777, 222}))
        with self.assertRaises(ValueError):
            parse_chat_ids("any")
        with self.assertRaises(ValueError):
            parse_chat_ids("0")

    def test_env_requires_credentials_and_allowlist(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            UserSessionConfig.from_environment()
        with patch.dict(os.environ, {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "example",
            "NOYA_USER_ALLOWED_CHAT_IDS": "-100777",
            "NOYA_USER_HISTORY_SCAN": "9999",
            "NOYA_USER_HISTORY_LINES": "999",
        }, clear=True):
            c = UserSessionConfig.from_environment()
            self.assertEqual(c.history_scan, 200)
            self.assertEqual(c.history_lines, 30)
            self.assertFalse(c.private_replies)
            self.assertFalse(c.agent_enabled)

    def test_memory_envelope_is_bot_api_independent(self):
        msg = SimpleNamespace(
            id=9, sender_id=222, raw_text="سلام",
            date=datetime.now(timezone.utc), edit_date=None,
        )
        sender = SimpleNamespace(
            first_name="Test", last_name="User", username="tester", bot=False,
        )
        envelope = _envelope(msg, sender=sender, chat_id=-100777, chat_type="supergroup")
        self.assertEqual(envelope.from_user.id, 222)
        self.assertEqual(envelope.from_user.full_name, "Test User")
        self.assertEqual(envelope.chat.type, "supergroup")
        self.assertEqual(envelope.message_id, 9)


class UserSessionSendTests(SimpleTestCase):
    async def test_long_text_splits_into_telegram_safe_chunks(self):
        client = SimpleNamespace(send_message=AsyncMock())
        runner = NoyaUserSession(client, make_config(), 999, "myuser")
        await runner._send_text(-100777, 12, "x" * 8100)
        self.assertEqual(client.send_message.await_count, 3)
        self.assertEqual(
            "".join(args.args[1] for args in client.send_message.await_args_list),
            "x" * 8100,
        )
        self.assertTrue(all(
            len(args.args[1]) <= 3900 for args in client.send_message.await_args_list
        ))

    async def test_wait_notice_removed_after_operation(self):
        client = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(id=44)),
            delete_messages=AsyncMock(),
        )
        runner = NoyaUserSession(client, make_config(), 999, "myuser")
        async def quick_notice():
            await runner._with_notice(-100777, 12, AsyncMock(return_value="ok")())
        with patch("botapp.user_session.asyncio.sleep", new=AsyncMock()) as sleep:
            await quick_notice()
            self.assertTrue(sleep.await_count >= 0)
        # Completion before 60 seconds does not generate a waiting message.
        client.delete_messages.assert_not_awaited()

    async def test_history_bounded_and_untrusted(self):
        class Client:
            async def iter_messages(self, chat_id, *, limit, offset_id):
                assert limit == 200
                assert offset_id == 40
                for i in (39, 38, 37):
                    yield SimpleNamespace(id=i, raw_text=f"message {i}", media=None, sender=None)
        runner = NoyaUserSession(Client(), make_config(), 999, "me")
        payload = await runner._history(-100777, 40)
        self.assertIn("UNTRUSTED_CHAT_HISTORY", payload)
        self.assertLess(payload.index("message 37"), payload.index("message 39"))
