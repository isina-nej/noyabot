"""Live Telegram regressions: archive/vision requests stay on Noya chat.

All tests use in-memory Telegram envelopes and the Django test database;
no real provider, Telegram credentials, or remote chat access.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.test import TestCase, override_settings

from botapp.agent.parser import (
    is_message_analysis_request, is_scoped_archive_chat_request,
)
from botapp.agent.classify import should_route_to_agent
from botapp.agent_handlers import AgentTriggerFilter
from botapp.models import MessageSnapshot
from botapp.noya_context import build_noya_user_payload


REQUEST = (
    "نویا، عکس فال پیام https://t.me/c/3861069387/93687 رو بررسی کن. "
    "ببین درخواست فال رو چه کسی داده. اگر از روی پیام‌ها مشخص شد، "
    "روی پیام درخواست‌کننده ریپلای کن و تگش کن و طبق معنی فال "
    "دوستانه نصیحتش کن."
)


def _message(*, text=REQUEST, message_id=93758):
    return SimpleNamespace(
        text=text, caption=None, message_id=message_id,
        reply_to_message=None,
        chat=SimpleNamespace(id=-1003861069387, type="supergroup", title="test"),
        from_user=SimpleNamespace(
            id=1, first_name="Sina", full_name="Sina", username="example",
        ),
    )


class ArchiveRoutingTests(TestCase):
    def test_specific_fortune_link_request_is_not_an_admin_command(self):
        self.assertTrue(is_scoped_archive_chat_request(REQUEST))
        # The older generic parser misses this shape; scoped routing must
        # cover it independently without silently changing admin heuristics.
        self.assertFalse(is_message_analysis_request(REQUEST))
        self.assertFalse(is_scoped_archive_chat_request("نویا تعداد ادمین های گروه چنده"))
        self.assertFalse(is_scoped_archive_chat_request(
            "نویا پیام https://t.me/c/3861069387/93687 رو پین کن"
        ))

    def test_classifier_never_asks_jev_to_route_archive_investigation(self):
        with patch(
            "botapp.agent.route_classifier.classify_request",
            side_effect=AssertionError("archive request must skip model router"),
        ):
            result = async_to_sync(should_route_to_agent)(
                REQUEST, chat_id=-1003861069387, has_reply=False,
            )
        self.assertFalse(result)

    def test_plain_noya_prefix_not_intercepted_by_admin_trigger(self):
        message = _message()
        with patch(
            "botapp.agent_handlers.should_route_to_agent",
            side_effect=AssertionError("must fall through to chat tools"),
        ), patch(
            "botapp.agent_handlers.resolve_admin_identity",
            side_effect=AssertionError("archive messages do not need admin identity"),
        ):
            routed = async_to_sync(AgentTriggerFilter().__call__)(message, None)
        self.assertFalse(routed)

    def test_real_admin_tasks_still_use_admin_routing(self):
        self.assertFalse(is_scoped_archive_chat_request(
            "نویا، مدیریت گروه رو بررسی کن و تنظیمات رو بگو"
        ))
        self.assertFalse(is_scoped_archive_chat_request(
            "نویا، پیام https://t.me/c/3861069387/93687 رو حذف کن"
        ))

    @override_settings(MESSAGE_ARCHIVE_ENABLED=True)
    def test_empty_live_context_reads_archived_messages_without_async_orm_error(self):
        from botapp import noya_context
        MessageSnapshot.objects.create(
            chat_id=-1003861069387, thread_id=0, message_id=93686,
            text="فال", sender_name="نیک‌نام",
        )
        current = _message(text="نویا قبلش چی گفته بود؟")
        with patch(
            "botapp.noya_context.recent_chat_context_lines", return_value=[],
        ):
            async def do_read():
                # This is the thread-sensitive adapter used by runbot.py.
                return await sync_to_async(
                    build_noya_user_payload, thread_sensitive=True,
                )(current, "قبلش چی گفته بود؟", archived_context="")
            payload = async_to_sync(do_read)()
        self.assertIn("نیک‌نام: فال", payload)
        self.assertIn("93686", payload)
        self.assertIn("[گفتگوی اخیر گروه]", payload)

    def test_analysis_payload_does_not_prohibit_authentic_chat_tools(self):
        payload = build_noya_user_payload(
            _message(), REQUEST, include_recent=False,
        )
        self.assertIn("get_group_message", payload)
        self.assertIn("trace_group_message", payload)
        self.assertIn("inspect_group_image", payload)
        self.assertIn("reply_to_group_message", payload)
        self.assertNotIn("هیچ ابزار یا دستور مدیریتی یا تصویری را اجرا نکن", payload)
        self.assertIn("غیرقابل‌اعتماد", payload)
