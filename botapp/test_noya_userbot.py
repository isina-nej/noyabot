"""Telethon user-session transport: no Telegram network, no private session data."""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shutil
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.noya_userbot import (
    NoyaUserbot, UserbotConfig, _content_type, _read_media, _same_chat_reference,
    _voice_file,
    _short_chunks, should_answer,
)


def _config(*, allowed=frozenset({-1001234567890}), saved=True):
    return UserbotConfig(
        api_id=123, api_hash="private-hash", session_path=Path("/tmp/noya-user"),
        allowed_chat_ids=frozenset(allowed), saved_messages_enabled=saved,
    )


def _event(
    text="نویا سلام", *, chat_id=-1001234567890, is_private=False,
    out=False, sender_id=200, reply=None, media=None,
):
    sender = SimpleNamespace(
        id=sender_id, first_name="Reyhaneh", last_name="",
        username="rey", bot=False,
    )
    msg = SimpleNamespace(
        id=120, date=None, media=media, raw_text=text,
        reply_to_msg_id=(reply.id if reply else None),
        sender_id=sender_id, file=SimpleNamespace(size=2000),
        photo=None, document=None,
    )
    ev = SimpleNamespace(
        chat_id=chat_id, id=120, raw_text=text, is_private=is_private,
        is_group=not is_private, out=out, sender_id=sender_id,
        is_reply=reply is not None,
        message=msg,
        input_chat=f"peer:{chat_id}",
        get_sender=AsyncMock(return_value=sender),
        get_chat=AsyncMock(return_value=SimpleNamespace(title="Trusted group")),
        get_reply_message=AsyncMock(return_value=reply),
    )
    return ev


def _client(*, history=None):
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(id=600)),
        send_file=AsyncMock(return_value=SimpleNamespace(id=601)),
        delete_messages=AsyncMock(),
        download_media=AsyncMock(return_value=b"media"),
        get_messages=AsyncMock(return_value=history or []),
    )


def _bot(*, config=None, client=None):
    return NoyaUserbot(
        client or _client(), config or _config(),
        owner_id=444, username="noya_account",
    )


class UserbotConfigTests(SimpleTestCase):
    def test_requires_explicit_absolute_session_and_credentials(self):
        env = {
            "NOYA_USERBOT_API_ID": "12345",
            "NOYA_USERBOT_API_HASH": "private-hash",
            "NOYA_USERBOT_SESSION_PATH": "/var/lib/noya-account/main",
            "NOYA_USERBOT_ALLOWED_CHAT_IDS": "-1001234567890,123456",
            "NOYA_USERBOT_SAVED_MESSAGES": "false",
        }
        cfg = UserbotConfig.from_env(env)
        self.assertEqual(cfg.allowed_chat_ids, {-1001234567890, 123456})
        self.assertEqual(cfg.session_path, Path("/var/lib/noya-account/main"))
        self.assertFalse(cfg.saved_messages_enabled)
        self.assertNotIn("private-hash", repr(cfg))
        for changes in (
            {"NOYA_USERBOT_SESSION_PATH": "relative.session"},
            {"NOYA_USERBOT_ALLOWED_CHAT_IDS": "all"},
            {"NOYA_USERBOT_API_HASH": ""},
            {"TELETHON_SESSION_PATH": "/var/lib/noya-account/main"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    UserbotConfig.from_env({**env, **changes})

    def test_default_never_grants_automatic_dialog_access(self):
        cfg = UserbotConfig.from_env({
            "TELETHON_API_ID": "12345",
            "TELETHON_API_HASH": "hash",
            "NOYA_USERBOT_SESSION_PATH": "/var/lib/noya-account/main",
        })
        self.assertEqual(cfg.allowed_chat_ids, frozenset())
        self.assertTrue(cfg.saved_messages_enabled)

    def test_management_commands_are_independent_from_botfather_tokens(self):
        from botapp.management.commands.login_noya_userbot import Command as Login
        from botapp.management.commands.run_noya_userbot import Command as Run
        self.assertIn("user session", Run.help)
        self.assertIn("Interactively", Login.help)
        with patch.dict(os.environ, {
            "NOYA_USERBOT_API_ID": "123", "NOYA_USERBOT_API_HASH": "hash",
            "NOYA_USERBOT_SESSION_PATH": "/tmp/nonexistent-account-session",
        }), self.assertRaises(Exception):
            Run().handle()


class UserbotTriggerTests(SimpleTestCase):
    def test_no_unlisted_groups_even_with_mention(self):
        cfg = _config(allowed=frozenset())
        for text in ("نویا سلام", "@noya_account سلام", "/noya سلام"):
            self.assertFalse(should_answer(_event(text), cfg, owner_id=444))

    def test_group_requires_explicit_trigger_reply_or_mention(self):
        config = _config()
        for text in ("نویا سلام", "noya hello", "/noya سلام", "@noya_account سلام"):
            self.assertTrue(
                should_answer(_event(text), config, owner_id=444, username="noya_account"),
                text,
            )
        self.assertFalse(should_answer(
            _event("سلام دوستان"), config, owner_id=444,
        ))
        self.assertTrue(should_answer(
            _event("درسته؟"), config, owner_id=444, replied_to_owner=True,
        ))
        self.assertFalse(should_answer(
            _event("نویا"), config, owner_id=444, sender_is_bot=True,
        ))
        self.assertFalse(should_answer(
            _event("نویا", out=True), config, owner_id=444,
        ))

    def test_dm_only_if_allowlisted_saved_messages_needs_slash_command(self):
        cfg = _config(allowed={222})
        self.assertTrue(should_answer(
            _event("سلام", chat_id=222, is_private=True), cfg, owner_id=444,
        ))
        self.assertFalse(should_answer(
            _event("نویا", chat_id=333, is_private=True), cfg, owner_id=444,
        ))
        self.assertFalse(should_answer(
            _event("نویا", chat_id=444, is_private=True, out=True),
            cfg, owner_id=444,
        ))
        self.assertTrue(should_answer(
            _event("/noya این چیه؟", chat_id=444, is_private=True, out=True),
            cfg, owner_id=444,
        ))
        self.assertFalse(should_answer(
            _event("/noya hi", chat_id=444, is_private=True, out=True),
            _config(saved=False), owner_id=444,
        ))

    def test_agent_text_is_bounded(self):
        self.assertEqual(_short_chunks("x" * 11_000)[0], "x" * 3400)
        self.assertLessEqual(len(_short_chunks("x" * 50000)), 6)


class SameChatPermalinkTests(SimpleTestCase):
    def test_same_group_private_supergroup_link(self):
        self.assertEqual(
            _same_chat_reference(
                "نویا https://t.me/c/3861069387/93764 رو ببین",
                chat_id=-1003861069387,
            ), 93764,
        )
        self.assertIsNone(_same_chat_reference(
            "https://t.me/c/3861069387/93764", chat_id=-100555555,
        ))
        self.assertIsNone(_same_chat_reference(
            "https://t.me/c/3861069387/93764", chat_id=123456,
        ))

    def test_public_group_permalink_must_match_username(self):
        self.assertEqual(
            _same_chat_reference(
                "https://t.me/our_noya_group/123", chat_id=-1005555,
                chat_username="our_noya_group",
            ), 123,
        )
        self.assertIsNone(_same_chat_reference(
            "https://t.me/other_group/123", chat_id=-1005555,
            chat_username="our_noya_group",
        ))

    def test_retrieves_old_media_from_current_peer_only(self):
        c = _client()
        old = SimpleNamespace(
            id=91, sender_id=999,
            raw_text="مدرک قدیمی", media=object(),
            photo=object(), document=None,
            file=SimpleNamespace(size=80),
        )
        c.get_messages.side_effect = [old, []]
        runtime = _bot(client=c)
        event = _event("نویا https://t.me/c/1234567890/91 رو بررسی کن")
        async def provider_runner():
            async def fake_run_memory(fake, question, provider, *, session_id, images=None):
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0]["source"], "reply")
                self.assertIn("مدرک قدیمی", question)
                return "انجام شد", {}
            with patch(
                "botapp.memory.integration.run_ai_with_memory",
                new=fake_run_memory,
            ), patch(
                "botapp.telegram_media.normalize_image_bytes",
                return_value=("image/jpeg", b"jpeg"),
            ):
                return await runtime._answer(event, None)
        result = async_to_sync(provider_runner)()
        self.assertEqual(result[0], "انجام شد")
        c.get_messages.assert_any_await(
            event.input_chat, ids=91,
        )
        c.download_media.assert_awaited_once_with(old, file=bytes)

    def test_fake_cross_chat_link_never_fetches_foreign_media(self):
        c = _client()
        runtime = _bot(client=c)
        event = _event("نویا https://t.me/c/9999999999/91 رو بررسی کن")
        async def fake_run_memory(fake, question, provider, *, session_id, images=None):
            self.assertFalse(images)
            return "خارج از این چت", {}
        with patch("botapp.memory.integration.run_ai_with_memory",
                   new=fake_run_memory):
            async_to_sync(runtime._answer)(event, None)
        # Access to current dialog history is okay, accessing a foreign
        # message ID or peer from an unverified link is not.
        self.assertFalse(any(
            "ids" in call.kwargs for call in c.get_messages.await_args_list
        ))


class UserbotDeliveryTests(SimpleTestCase):
    def test_plain_text_is_reply_to_authenticated_message(self):
        c = _client()
        bot = _bot(client=c)
        event = _event()
        sent = async_to_sync(bot._deliver)(event, "سلام عزیزم!", {})
        self.assertTrue(sent)
        c.send_message.assert_awaited_once_with(
            event.input_chat, "سلام عزیزم!", reply_to=event.id,
        )
        c.send_file.assert_not_awaited()

    def test_agent_staged_messages_preserve_reply_modes(self):
        c = _client()
        c.send_message.side_effect = [
            SimpleNamespace(id=701), SimpleNamespace(id=702),
            SimpleNamespace(id=703),
        ]
        bot = _bot(client=c)
        e = _event()
        meta = {"queued_chat_messages": [
            {"text": "اول", "reply_to": "current"},
            {"text": "دوم", "reply_to": "none"},
            {"text": "سوم", "reply_to": "previous"},
        ]}
        self.assertTrue(async_to_sync(bot._deliver)(e, "", meta))
        self.assertEqual(
            [x.kwargs.get("reply_to") for x in c.send_message.await_args_list],
            [120, None, 702],
        )

    def test_generated_tts_encodes_real_ogg_opus_voice_note(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required for Telegram voice encoding")
        wav = io.BytesIO()
        with wave.open(wav, "wb") as voice:
            voice.setnchannels(1)
            voice.setsampwidth(2)
            voice.setframerate(16000)
            voice.writeframes(b"\\x00\\x00" * 8000)
        stream = async_to_sync(_voice_file)(wav.getvalue())
        self.assertEqual(stream.name, "noya_voice.ogg")
        self.assertTrue(stream.read(4).startswith(b"OggS"))

    def test_generated_image_and_audio_use_same_authenticated_peer(self):
        c = _client()
        bot = _bot(client=c)
        event = _event()
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nimg").decode()
        audio = b"fake-tts-audio"
        buffer = io.BytesIO(b"fake-ogg")
        buffer.name = "voice.ogg"
        with patch("botapp.noya_userbot._voice_file", new=AsyncMock(return_value=buffer)):
            result = async_to_sync(bot._deliver)(event, "", {
                "generated_image_b64": image,
                "generated_tts_audio": audio,
            })
        self.assertTrue(result)
        self.assertEqual(c.send_file.await_count, 2)
        args1 = c.send_file.await_args_list[0]
        args2 = c.send_file.await_args_list[1]
        self.assertEqual(args1.args[0], event.input_chat)
        self.assertFalse(args1.kwargs["force_document"])
        self.assertEqual(args1.kwargs["reply_to"], event.id)
        self.assertEqual(args2.args[0], event.input_chat)
        self.assertTrue(args2.kwargs["voice_note"])

    def test_media_sequence_text_voice_image_in_order_without_duplicate(self):
        c = _client()
        bot = _bot(client=c)
        event = _event()
        buffer = io.BytesIO(b"ogg")
        buffer.name = "voice.ogg"
        meta = {"queued_media_sequence": [
            {"kind": "text", "text": "نوشته", "reply_to": "current"},
            {"kind": "voice", "bytes": b"mp3", "reply_to": "previous"},
            {"kind": "image", "bytes": b"\xff\xd8image", "reply_to": "none"},
        ]}
        with patch("botapp.noya_userbot._voice_file", new=AsyncMock(return_value=buffer)):
            self.assertTrue(async_to_sync(bot._deliver)(event, "اجرا شد", meta))
        self.assertEqual(c.send_message.await_count, 1)
        self.assertEqual(c.send_file.await_count, 2)
        self.assertEqual(c.send_file.await_args_list[0].kwargs["reply_to"], 600)
        self.assertIsNone(c.send_file.await_args_list[1].kwargs["reply_to"])

    def test_archived_reply_uses_verified_target_in_current_chat_only(self):
        c = _client()
        runtime = _bot(client=c)
        e = _event()
        self.assertTrue(async_to_sync(runtime._deliver)(e, "", {
            "queued_chat_messages": [{
                "reply_to": "archived", "text": "موفق باشی",
                "verified_group_reply": True,
                "target_message_id": 110, "mention_user_id": 1234,
                "mention_label": "ریحانه",
            }],
        }))
        call = c.send_message.await_args
        self.assertEqual(call.args[0], e.input_chat)
        self.assertEqual(call.kwargs["reply_to"], 110)
        self.assertEqual(call.kwargs["parse_mode"], "html")
        self.assertIn("tg://user?id=1234", call.args[1])

    def test_unverified_archive_reply_never_sends(self):
        c = _client()
        runtime = _bot(client=c)
        e = _event()
        async_to_sync(runtime._deliver)(e, "", {
            "queued_chat_messages": [{
                "reply_to": "archived", "text": "secret",
                "verified_group_reply": False,
                "target_message_id": 110,
            }],
        })
        c.send_message.assert_not_awaited()

    def test_provider_receives_real_authenticated_scope_not_shadow_id(self):
        c = _client()
        runtime = _bot(client=c)
        ev = _event("نویا جواب بده")
        recorded = {}

        async def fake_provider_agent(**kwargs):
            recorded.update(kwargs)
            return "سلام", {}

        async def fake_memory(fake, question, provider, *, session_id, images=None):
            self.assertNotEqual(fake.chat.id, ev.chat_id)
            return await provider(
                question, session_id, speaker_user_id=ev.sender_id,
                speaker_name="ریحانه", reaction_allowed=False,
            )
        async def run():
            with patch(
                "botapp.memory.integration.run_ai_with_memory", new=fake_memory,
            ), patch(
                "botapp.agent.loop.run_agent_loop",
                new=fake_provider_agent,
            ):
                return await runtime._answer(ev, None)
        text, _meta = async_to_sync(run)()
        self.assertEqual(text, "سلام")
        scope = recorded["telegram_context"]
        self.assertEqual(scope["chat_id"], ev.chat_id)
        self.assertIs(scope["bot"], c)
        self.assertEqual(scope["current_message_id"], ev.id)

    def test_reply_only_if_final_output_was_sent(self):
        c = _client()
        c.send_message.side_effect = RuntimeError("Telegram send rejected")
        bot = _bot(client=c)
        with self.assertRaises(RuntimeError):
            async_to_sync(bot._deliver)(_event(), "final", {})
        self.assertFalse(async_to_sync(bot._deliver)(
            _event(), "", {"no_reply": True},
        ))

    def test_one_minute_wait_note_deletes_after_success(self):
        c = _client()
        runtime = _bot(client=c)
        event = _event()
        async def fake_answer(_event, _reply):
            await asyncio.sleep(0)
            return "متن نهایی", {}
        runtime._answer = fake_answer
        async def fast_wait(_event):
            return SimpleNamespace(id=855)
        runtime._wait_notice = fast_wait
        async_to_sync(runtime.on_message)(event)
        c.send_message.assert_awaited_once()
        c.delete_messages.assert_awaited_once_with(event.input_chat, [855])

    def test_wait_notice_stays_if_sending_final_fails(self):
        c = _client()
        c.send_message.side_effect = RuntimeError("Offline")
        runtime = _bot(client=c)
        event = _event()
        runtime._answer = AsyncMock(return_value=("final", {}))
        runtime._wait_notice = AsyncMock(return_value=SimpleNamespace(id=855))
        async_to_sync(runtime.on_message)(event)
        c.delete_messages.assert_not_awaited()

    def test_unauthorized_chat_never_reads_or_sends_messages(self):
        c = _client()
        runtime = _bot(client=c, config=_config(allowed=frozenset()))
        async_to_sync(runtime.on_message)(_event())
        c.get_messages.assert_not_awaited()
        c.send_message.assert_not_awaited()

    def test_recent_history_uses_only_current_authorized_peer(self):
        history = [SimpleNamespace(
            id=111, sender_id=42, raw_text="از آخرین موضوع گروه",
        )]
        c = _client(history=history)
        runtime = _bot(client=c)
        e = _event()
        answer = async_to_sync(runtime._history)(e)
        self.assertIn("از آخرین", answer)
        c.get_messages.assert_awaited_once_with(
            e.input_chat, limit=24, offset_id=e.id,
        )


class UserbotMediaTests(SimpleTestCase):
    def test_document_media_classification_for_video_note_voice_and_gif(self):
        from telethon.tl.types import (
            DocumentAttributeAnimated, DocumentAttributeAudio,
            DocumentAttributeVideo,
        )
        doc = SimpleNamespace(mime_type="video/mp4", attributes=[
            DocumentAttributeVideo(duration=12.0, w=400, h=400, round_message=True),
        ])
        self.assertEqual(_content_type(SimpleNamespace(photo=None, document=doc)), "video_note")
        doc.attributes = [DocumentAttributeAudio(duration=3, voice=True)]
        self.assertEqual(_content_type(SimpleNamespace(photo=None, document=doc)), "voice")
        doc.attributes = [DocumentAttributeAnimated()]
        self.assertEqual(_content_type(SimpleNamespace(photo=None, document=doc)), "animation")

    def test_photo_is_downloaded_only_on_explicitly_addressed_event(self):
        c = _client()
        message = SimpleNamespace(
            file=SimpleNamespace(size=200), media=object(), photo=object(),
            document=None,
        )
        with patch(
            "botapp.telegram_media.normalize_image_bytes",
            return_value=("image/jpeg", b"jpeg"),
        ):
            frames, text, warning = async_to_sync(_read_media)(c, message, source="reply")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["source"], "reply")
        self.assertEqual(text, "")
        c.download_media.assert_awaited_once_with(message, file=bytes)

    def test_oversize_attachment_rejected_before_download(self):
        c = _client()
        message = SimpleNamespace(
            file=SimpleNamespace(size=25_000_000), media=object(), photo=object(),
            document=None,
        )
        frames, text, warnings = async_to_sync(_read_media)(c, message, source="reply")
        self.assertFalse(frames)
        self.assertTrue(warnings)
        c.download_media.assert_not_awaited()

    def test_video_note_reuses_three_frames_and_stt(self):
        from telethon.tl.types import DocumentAttributeVideo
        c = _client()
        msg = SimpleNamespace(
            file=SimpleNamespace(size=1000), media=object(), photo=None,
            document=SimpleNamespace(
                mime_type="video/mp4",
                attributes=[DocumentAttributeVideo(duration=8.0, w=320, h=320, round_message=True)],
            ),
        )
        with patch(
            "botapp.animated_media.sample_animation_frames",
            new=AsyncMock(return_value=[("image/jpeg", b"frame")] * 3),
        ), patch(
            "botapp.media_intake._ffmpeg",
            new=AsyncMock(return_value=b"audio-mp3"),
        ), patch(
            "botapp.media_intake.transcribe_audio",
            new=AsyncMock(return_value="سلام این یک ویدیو است"),
        ):
            frames, spoken, _ = async_to_sync(_read_media)(c, msg, source="reply")
        self.assertEqual(len(frames), 3)
        self.assertIn("سلام", spoken)
