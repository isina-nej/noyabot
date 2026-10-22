"""Tests for channel behaviour parity + MTProto channel analysis.

Covered:
* «نویا» in a channel runs the same pipeline as groups (memory-aware agent
  path, reply context, images) instead of a bare API call.
* «نویا تحلیل» fetches the channel digest through MTProto and asks the model
  for a *narrative* analysis (no list output).
* Failure of MTProto is reported honestly — no fabricated analysis.
* The digest aggregates views / forwards / reactions / authors / topics and
  keeps message bodies out of the prompt (only short excerpts).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from botapp.channel_insights import (
    ChannelInsightUnavailable,
    ChannelMessageMeta,
    analysis_limit_from_text,
    build_analysis_prompt,
    build_digest,
    wants_channel_analysis,
)
from botapp.nouya_handler import (
    channel_nouya_handler,
    router,
    split_telegram_text,
)


def _meta(
    *,
    day: int = 1,
    views: int = 100,
    forwards: int = 0,
    reactions: int = 0,
    author: str = "noya",
    media: str = "text",
    excerpt: str = "سلام",
) -> ChannelMessageMeta:
    return ChannelMessageMeta(
        date=datetime(2026, 1, day, 12, 0, tzinfo=timezone.utc),
        views=views,
        forwards=forwards,
        reactions=reactions,
        author=author,
        media=media,
        excerpt=excerpt,
    )


class ChannelAnalysisDetectionTests(SimpleTestCase):
    def test_analysis_request_detected(self):
        self.assertTrue(wants_channel_analysis("تحلیل کن"))
        self.assertTrue(wants_channel_analysis("لطفا تحلیل کن 1000 پیام آخر رو"))
        self.assertTrue(wants_channel_analysis("analysis of this channel"))

    def test_single_message_analysis_not_channel_wide(self):
        self.assertFalse(wants_channel_analysis("این پست رو تحلیل کن"))
        self.assertFalse(wants_channel_analysis("همین پیام رو تحلیل کن"))

    def test_plain_question_not_analysis(self):
        self.assertFalse(wants_channel_analysis("سلام چه خبر"))
        self.assertFalse(wants_channel_analysis(""))

    def test_limit_parsing(self):
        self.assertEqual(analysis_limit_from_text("۱۰۰۰ تا پیام آخر"), 1000)
        self.assertEqual(analysis_limit_from_text("500 پیام آخر"), 500)
        self.assertEqual(analysis_limit_from_text("بدون عدد"), 1000)
        # Out-of-range values are clamped to a safe window.
        self.assertEqual(analysis_limit_from_text("999999 پیام"), 3000)
        self.assertEqual(analysis_limit_from_text("2 پیام"), 50)


class ChannelDigestTests(SimpleTestCase):
    def test_digest_aggregates_metadata(self):
        metas = [
            _meta(day=1, views=10, forwards=1, reactions=2, author="ali", excerpt="#هک #پایتون"),
            _meta(day=2, views=300, forwards=9, reactions=40, author="sara", excerpt="پایتون خوبه"),
            _meta(day=3, views=50, forwards=0, reactions=0, author="ali", media="photo", excerpt="عکس"),
        ]
        digest = build_digest(chat_id=-1001, title="کانال تست", metas=metas, requested_limit=1000)

        self.assertEqual(digest.message_count, 3)
        self.assertEqual(digest.total_views, 360)
        self.assertEqual(digest.total_forwards, 10)
        self.assertEqual(digest.total_reactions, 42)
        self.assertEqual(digest.top_by_views[0]["views"], 300)
        self.assertEqual(digest.top_by_forwards[0]["forwards"], 9)
        self.assertEqual(digest.top_by_reactions[0]["reactions"], 40)
        self.assertEqual(digest.top_authors[0][0], "ali")
        self.assertEqual(digest.top_authors[0][1], 2)
        self.assertEqual(digest.media_mix.get("photo"), 1)
        self.assertTrue(digest.top_topics)

    def test_evidence_block_is_untrusted_and_bounded(self):
        digest = build_digest(
            chat_id=-1001,
            title="کانال تست",
            metas=[_meta(excerpt="متن نمونه") for _ in range(3)],
            requested_limit=1000,
        )
        block = digest.evidence_block()
        self.assertIn("<external_source", block)
        self.assertIn("UNTRUSTED", block)
        self.assertLessEqual(len(block), 7500)

    def test_analysis_prompt_demands_narrative_not_list(self):
        digest = build_digest(
            chat_id=-1001, title="کانال تست", metas=[_meta()], requested_limit=1000
        )
        prompt = build_analysis_prompt(digest, "تحلیل کن")
        self.assertIn("روایی", prompt)
        self.assertIn("بدون فهرست", prompt)
        self.assertIn("<external_source", prompt)
        self.assertIn("تحلیل کن", prompt)

    def test_empty_digest_is_still_serializable(self):
        digest = build_digest(chat_id=-1001, title="", metas=[], requested_limit=1000)
        self.assertEqual(digest.message_count, 0)
        self.assertIsInstance(digest.evidence_block(), str)


class ChannelHandlerTests(SimpleTestCase):
    def _message(self, text: str = "@NuyaRobot سلام"):
        reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
        return SimpleNamespace(
            text=text,
            caption=None,
            rich_message=None,
            bot=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="NuyaRobot", id=7)),
                send_chat_action=AsyncMock(),
            ),
            chat=SimpleNamespace(id=-100, type="channel"),
            message_id=77,
            from_user=SimpleNamespace(id=42, full_name="Admin", first_name="Admin", username="admin"),
            reply_to_message=None,
            reply=reply,
            _reply_obj=SimpleNamespace(edit_text=AsyncMock()),
        )

    def test_channel_post_uses_memory_aware_pipeline(self):
        """The channel path must go through run_ai_with_memory like groups do."""
        message = self._message()
        message.reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))

        with patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(return_value=("پاسخ کانال", {})),
        ) as run_memory, patch(
            "botapp.nouya_handler.call_noya_api",
            new=AsyncMock(return_value=("پاسخ مستقیم", {})),
        ) as call_direct:
            import asyncio

            asyncio.run(channel_nouya_handler(message))

        run_memory.assert_awaited_once()
        call_direct.assert_not_awaited()
        _message_arg, payload, provider = run_memory.await_args.args
        self.assertIs(provider, call_direct)
        self.assertIn("channel", run_memory.await_args.kwargs["session_id"])

    def test_channel_post_reply_carries_context(self):
        message = self._message(text="@NuyaRobot این رو بخون")
        message.reply_to_message = SimpleNamespace(
            text="متن اصلی پست",
            caption=None,
            rich_message=None,
            from_user=SimpleNamespace(id=5, full_name="Ali", first_name="Ali", username="ali"),
            reply_to_message=None,
        )
        message.reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))

        captured = {}

        async def _fake_run(_message, payload, provider, session_id, **kwargs):
            captured["payload"] = payload
            captured["session_id"] = session_id
            return ("پاسخ", {})

        with patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(side_effect=_fake_run),
        ):
            import asyncio

            asyncio.run(channel_nouya_handler(message))

        self.assertIn("متن اصلی پست", captured["payload"])

    def test_channel_analysis_uses_digest_and_requests_narrative(self):
        message = self._message(text="@NuyaRobot تحلیل کن 1000 پیام آخر رو")
        message.reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
        digest = build_digest(
            chat_id=-100, title="کانال", metas=[_meta(views=10)], requested_limit=1000
        )

        captured = {}

        async def _fake_api(prompt, session_id, **kwargs):
            captured["prompt"] = prompt
            return ("تحلیل روایی", {})

        with patch(
            "botapp.nouya_handler.fetch_channel_digest",
            new=AsyncMock(return_value=digest),
        ) as fetch, patch(
            "botapp.nouya_handler.call_noya_api",
            new=AsyncMock(side_effect=_fake_api),
        ):
            import asyncio

            asyncio.run(channel_nouya_handler(message))

        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1], 1000)
        self.assertIn("روایی", captured["prompt"])
        self.assertIn("<external_source", captured["prompt"])

    def test_channel_analysis_failure_is_honest(self):
        message = self._message(text="@NuyaRobot تحلیل کن")
        message.reply = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))

        with patch(
            "botapp.nouya_handler.fetch_channel_digest",
            new=AsyncMock(side_effect=ChannelInsightUnavailable("mtproto_not_configured")),
        ), patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(return_value=("نباید صدا زده شود", {})),
        ) as run_memory:
            import asyncio

            asyncio.run(channel_nouya_handler(message))

        run_memory.assert_not_awaited()
        edited = message.reply.return_value.edit_text
        edited.assert_awaited_once()
        self.assertIn("MTProto", edited.await_args.args[0])

    def test_channel_post_ignored_without_address(self):
        message = self._message(text="یک پست عادی")
        message.reply = AsyncMock()
        with patch(
            "botapp.nouya_handler.run_ai_with_memory",
            new=AsyncMock(return_value=("نباید", {})),
        ) as run_memory:
            import asyncio

            asyncio.run(channel_nouya_handler(message))
        run_memory.assert_not_awaited()

    def test_channel_post_update_type_registered(self):
        self.assertIn("channel_post", router.resolve_used_update_types())


class SplitTelegramTextTests(SimpleTestCase):
    def test_short_text_is_single_chunk(self):
        self.assertEqual(split_telegram_text("سلام"), ["سلام"])

    def test_long_text_is_chunked_under_limit(self):
        chunks = split_telegram_text("خط " * 3000, limit=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)

    def test_empty_text(self):
        self.assertEqual(split_telegram_text(""), [])
        self.assertEqual(split_telegram_text("   "), [])
