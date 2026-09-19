from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from botapp.management.commands.runbot import (
    collaboration_disabled_message,
    command_argument,
    parse_action_arguments,
    plain_command,
    PLAIN_MODERATION_COMMANDS,
    split_delete_modifier,
    prompt_link,
    render_template,
)
from botapp.models import (
    BotMessageSettings,
    ChatLink,
    GroupQuota,
    GroupSchedule,
    GroupSettings,
    ModerationAction,
    ModerationLog,
    Warning,
)
from botapp.moderation import (
    authenticate_api_key,
    cancel_action,
    create_action,
    create_api_key,
    create_reversal,
    enqueue_daily_group_schedules,
    render_group_message,
    requeue_stale_processing_actions,
    validate_action_spec,
    warning_ceiling_spec,
)
from botapp.template_renderer import gregorian_to_jalali, render_member_template
from botapp.services import (
    call_ai_api,
    call_noya_api,
    consume_group_quota,
    contains_blocked_word,
    extract_urls,
    add_warning,
    clear_warnings,
    get_active_warning_count,
    is_allowed_url,
    is_duplicate_message,
    is_flooding,
)


class BotHelpersTest(TestCase):
    def test_prompt_link_uses_token(self):
        assert prompt_link("secure-token") == "https://ai.tinkera.org/chat/secure-token"

    def test_command_argument_supports_commands_with_bot_username(self):
        assert command_argument("/prompt@my_bot سوال من") == "سوال من"
        assert command_argument("/prompt") == ""

    def test_plain_commands_support_persian_and_no_slash(self):
        assert plain_command("اخطار دلیل تخلف") == ("اخطار", "دلیل تخلف")
        assert plain_command("mute spam") == ("mute", "spam")
        assert plain_command("/بن") == ("بن", "")
        assert PLAIN_MODERATION_COMMANDS["اخطار"] == "warn"
        assert PLAIN_MODERATION_COMMANDS["میوت"] == "mute"
        assert PLAIN_MODERATION_COMMANDS["بن"] == "ban"
        assert PLAIN_MODERATION_COMMANDS["آنبن"] == "unban"

    def test_disabled_message_is_fixed(self):
        assert collaboration_disabled_message() == "ادمین گزینه همکاری را غیر فعال کرده است."

    def test_render_template_escapes_name_and_channel(self):
        user = SimpleNamespace(id=7, first_name="<Ali>")
        result = render_template("{mention} / {channel} / {name}", user, "@Tinkera<Bot>")
        assert 'tg://user?id=7' in result
        assert '&lt;Ali&gt;' in result
        assert '@Tinkera&lt;Bot&gt;' in result

    def test_action_argument_and_delete_modifier_parsing(self):
        assert parse_action_arguments("60 30 spam") == (60, 30, "spam")
        assert parse_action_arguments("60 دائمی spam") == (60, None, "spam")
        assert split_delete_modifier("30 اسپم حذف") == ("30 اسپم", True)
        assert PLAIN_MODERATION_COMMANDS["حذف"] == "delete"
        assert PLAIN_MODERATION_COMMANDS["قفل"] == "lock"

    def test_welcome_placeholders_include_clickable_name_and_jalali_date(self):
        user = SimpleNamespace(id=7, first_name="<Ali>", full_name="<Ali>")
        now = timezone.make_aware(datetime(2026, 7, 22, 18, 52, 30))
        result = render_member_template(
            "#name | #title | #time | #date | #datesh",
            user,
            "<Group>",
            now,
        )
        assert 'tg://user?id=7' in result
        assert "&lt;Group&gt;" in result
        assert "18:52:30" in result
        assert "2026/07/22" in result
        assert "1405/04/31" in result
        assert gregorian_to_jalali(now.date()) == (1405, 4, 31)

    def test_default_welcome_template_is_requested_format(self):
        group = GroupSettings()
        assert group.welcome_message == (
            "سلام #name عزیز به گروه #title خوش آمدی 🌷 "
            "✅ ساعت: ( #time ) ✅ تاریخ: ( #date )"
        )

    def test_collaboration_defaults_enabled(self):
        assert BotMessageSettings().collaboration_enabled is True


class GroupQuotaTest(TestCase):
    def test_consume_group_quota_honors_limit(self):
        GroupQuota.objects.create(chat_id=10, daily_prompt_limit=2)

        assert async_to_sync(consume_group_quota)(10, "گروه") is True
        assert async_to_sync(consume_group_quota)(10, "گروه") is True
        assert async_to_sync(consume_group_quota)(10, "گروه") is False

        quota = GroupQuota.objects.get(chat_id=10)
        assert quota.tokens_used_today == 2
        assert quota.chat_title == "گروه"

    def test_consume_group_quota_zero_is_unlimited(self):
        GroupQuota.objects.create(chat_id=12, daily_prompt_limit=0, tokens_used_today=999)

        for _ in range(5):
            assert async_to_sync(consume_group_quota)(12, "گروه بی‌نهایت") is True

        quota = GroupQuota.objects.get(chat_id=12)
        assert quota.tokens_used_today == 999
        assert quota.daily_prompt_limit == 0

    def test_new_group_quota_defaults_to_unlimited(self):
        assert async_to_sync(consume_group_quota)(13, "گروه جدید") is True
        quota = GroupQuota.objects.get(chat_id=13)
        assert quota.daily_prompt_limit == 0

    def test_consume_group_quota_resets_on_new_day(self):
        GroupQuota.objects.create(
            chat_id=11,
            daily_prompt_limit=1,
            tokens_used_today=1,
            last_reset=timezone.localdate() - timedelta(days=1),
        )

        assert async_to_sync(consume_group_quota)(11) is True
        quota = GroupQuota.objects.get(chat_id=11)
        assert quota.tokens_used_today == 1
        assert quota.last_reset == timezone.localdate()


class ModerationRulesTest(TestCase):
    def test_blocked_words_are_case_insensitive(self):
        assert contains_blocked_word("این BAD Word است", ["bad word"]) is True
        assert contains_blocked_word("پیام سالم", ["bad"]) is False

    def test_blocked_words_ignore_zero_width_and_extra_spaces(self):
        assert contains_blocked_word("این ba\u200bd word است", ["bad word"]) is True
        assert contains_blocked_word("بد   کلمه", ["بد کلمه"]) is True

    def test_url_extraction_and_domain_allowlist(self):
        assert extract_urls("ببین https://sub.example.com/a و www.bad.test/x") == [
            "https://sub.example.com/a",
            "www.bad.test/x",
        ]
        assert "t.me/example" in extract_urls("عضو شوید t.me/example همین الان")
        assert "evil.com" in extract_urls("سایت evil.com را ببین")
        assert is_allowed_url("https://sub.example.com/a", ["example.com"]) is True
        assert is_allowed_url("https://fakeexample.com", ["example.com"]) is False

    def test_flood_limit_uses_sliding_window(self):
        base = timezone.now()
        assert is_flooding(1001, 7, 2, 10, base) is False
        assert is_flooding(1001, 7, 2, 10, base + timedelta(seconds=1)) is False
        assert is_flooding(1001, 7, 2, 10, base + timedelta(seconds=2)) is True
        assert is_flooding(1001, 7, 2, 10, base + timedelta(seconds=20)) is False

    def test_duplicate_message_limit(self):
        base = timezone.now()
        assert is_duplicate_message(1002, 8, "سلام", 3, 10, base) is False
        assert is_duplicate_message(1002, 8, "  سلام ", 3, 10, base + timedelta(seconds=1)) is False
        assert is_duplicate_message(1002, 8, "سلام", 3, 10, base + timedelta(seconds=2)) is True

    def test_group_moderation_defaults_are_safe(self):
        group = GroupSettings(chat_id=-100)
        assert group.moderation_enabled is True
        assert group.anti_spam_enabled is True
        assert group.anti_link_enabled is True
        assert group.anti_forward_enabled is False


class WarningTest(TestCase):
    def test_warning_cycle_resets_after_punishment(self):
        group = GroupSettings.objects.create(chat_id=-199, max_warnings=3)
        for _ in range(3):
            count = async_to_sync(add_warning)(
                group.id,
                1,
                "member",
                9,
                "admin",
                "spam",
                30,
            )
        assert count == 3
        assert async_to_sync(clear_warnings)(group.id, 1) == 3
        assert async_to_sync(get_active_warning_count)(group.id, 1) == 0
        assert async_to_sync(add_warning)(
            group.id,
            1,
            "member",
            9,
            "admin",
            "again",
            30,
        ) == 1

    def test_active_warning_count_ignores_expired_and_revoked(self):
        group = GroupSettings.objects.create(chat_id=-200)
        now = timezone.now()
        Warning.objects.create(
            group=group,
            user_id=1,
            issued_by_user_id=9,
            expires_at=now + timedelta(days=1),
        )
        Warning.objects.create(
            group=group,
            user_id=1,
            issued_by_user_id=9,
            expires_at=now - timedelta(seconds=1),
        )
        Warning.objects.create(
            group=group,
            user_id=1,
            issued_by_user_id=9,
            expires_at=now + timedelta(days=1),
            revoked_at=now,
        )
        assert async_to_sync(get_active_warning_count)(group.id, 1) == 1

    def test_moderation_log_persists_action(self):
        group = GroupSettings.objects.create(chat_id=-201)
        log = ModerationLog.objects.create(
            group=group,
            target_user_id=1,
            action="mute",
            reason="spam",
            duration_minutes=10,
        )
        assert log.action == "mute"
        assert group.moderation_logs.count() == 1


class ScheduledModerationTest(TestCase):
    def setUp(self):
        self.group = GroupSettings.objects.create(chat_id=-300, chat_title="test")

    def test_all_temporal_modes_share_one_action_model(self):
        immediate, _ = create_action(group=self.group, action="mute", target_user_id=1)
        timed, _ = create_action(
            group=self.group,
            action="mute",
            target_user_id=2,
            duration_minutes=30,
        )
        delayed, _ = create_action(
            group=self.group,
            action="ban",
            target_user_id=3,
            delay_minutes=60,
        )
        combined, _ = create_action(
            group=self.group,
            action="ban",
            target_user_id=4,
            delay_minutes=60,
            duration_minutes=30,
        )
        assert immediate.duration_minutes is None
        assert timed.duration_minutes == 30
        assert delayed.execute_at > delayed.created_at
        assert combined.duration_minutes == 30

    def test_idempotency_returns_existing_action(self):
        first, created = create_action(
            group=self.group,
            action="lock",
            idempotency_key="request-1",
        )
        second, created_again = create_action(
            group=self.group,
            action="lock",
            idempotency_key="request-1",
        )
        assert created is True
        assert created_again is False
        assert first.id == second.id

    def test_daily_group_schedule_enqueues_once_per_day(self):
        schedule = GroupSchedule.objects.create(
            group=self.group,
            action="lock",
            time_of_day=time(8, 0),
        )
        now = timezone.make_aware(datetime(2026, 7, 22, 8, 1))
        assert enqueue_daily_group_schedules(now) == 1
        assert enqueue_daily_group_schedules(now + timedelta(minutes=1)) == 0
        schedule.refresh_from_db()
        assert schedule.last_enqueued_date == now.date()
        assert self.group.actions.filter(action="lock", source="schedule").count() == 1

    def test_timed_action_creates_release(self):
        action, _ = create_action(
            group=self.group,
            action="ban",
            target_user_id=1,
            duration_minutes=45,
        )
        reversal = create_reversal(action, timezone.now())
        assert reversal.action == "unban"
        assert reversal.target_user_id == 1
        assert reversal.execute_at > timezone.now()

    def test_external_key_cannot_poison_reversal(self):
        action, _ = create_action(
            group=self.group,
            action="mute",
            target_user_id=7,
            duration_minutes=30,
        )
        # An untrusted caller tries to pre-book the internal reversal key so the
        # automatic unmute would silently be skipped (leaving the user muted).
        create_action(
            group=self.group, action="lock",
            idempotency_key=f"sys:reverse:{action.id}", source="api",
        )
        create_action(
            group=self.group, action="lock",
            idempotency_key=f"reverse:{action.id}", source="api",
        )
        reversal = create_reversal(action, timezone.now())
        assert reversal is not None
        assert reversal.action == "unmute"
        assert reversal.target_user_id == 7

    def test_external_key_cannot_poison_daily_schedule(self):
        schedule = GroupSchedule.objects.create(
            group=self.group, action="lock", time_of_day=time(8, 0),
        )
        now = timezone.make_aware(datetime(2026, 7, 22, 8, 1))
        create_action(
            group=self.group, action="unlock",
            idempotency_key=f"sys:daily:{schedule.id}:{now.date().isoformat()}",
            source="api",
        )
        assert enqueue_daily_group_schedules(now) == 1
        assert self.group.actions.filter(action="lock", source="schedule").count() == 1

    def test_cancel_only_pending_action(self):
        action, _ = create_action(group=self.group, action="lock")
        cancel_action(action)
        assert action.status == "cancelled"
        with self.assertRaises(ValueError):
            cancel_action(action)

    def test_warning_ceiling_has_one_configured_action(self):
        self.group.max_warnings_action = "ban"
        self.group.max_warnings_action_delay_minutes = 60
        self.group.max_warnings_action_duration_minutes = 30
        spec = warning_ceiling_spec(self.group)
        assert spec.action == "ban"
        assert spec.delay_minutes == 60
        assert spec.duration_minutes == 30

    def test_template_override_and_fallback(self):
        self.group.message_templates = {"ban": "{target} اخراج شد"}
        assert render_group_message(self.group, "ban", target="Ali") == "Ali اخراج شد"
        assert "سکوت" in render_group_message(self.group, "mute", target="Ali")

    def test_template_with_positional_placeholder_falls_back_instead_of_crashing(self):
        self.group.message_templates = {"ban": "{0} اخراج شد"}
        assert render_group_message(self.group, "ban", target="Ali") == "Ali بن شد."

    def test_action_validation(self):
        assert validate_action_spec("mute", 60, 30).is_delayed is True
        with self.assertRaises(ValueError):
            validate_action_spec("unmute", 0, 30)
        with self.assertRaises(ValueError):
            create_action(group=self.group, action="ban")


class ModerationApiTest(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="staff",
            password="pass",
            is_staff=True,
        )
        self.group = GroupSettings.objects.create(chat_id=-400)

    def test_api_requires_authentication(self):
        response = self.client.post(
            reverse("create-action-api"),
            data={"chat_id": -400, "action": "lock"},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_staff_can_create_delayed_timed_action(self):
        _, raw = create_api_key("create-action", self.staff)
        response = self.client.post(
            reverse("create-action-api"),
            data={
                "chat_id": -400,
                "action": "ban",
                "target_user_id": 7,
                "delay_minutes": 60,
                "duration_minutes": 30,
            },
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="api-request-1",
            HTTP_X_API_KEY=raw,
        )
        assert response.status_code == 201
        data = response.json()
        assert data["action"] == "ban"
        assert data["duration_minutes"] == 30

    def test_api_key_authenticates(self):
        _, raw = create_api_key("automation", self.staff)
        assert authenticate_api_key(raw) is not None
        response = self.client.get(
            reverse("group-settings-api", args=[-400]),
            HTTP_X_API_KEY=raw,
        )
        assert response.status_code == 200

    def test_staff_session_write_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        response = csrf_client.post(
            reverse("group-settings-api", args=[-400]),
            data={"max_warnings": 4},
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_staff_can_update_policy_and_templates(self):
        _, raw = create_api_key("settings-write", self.staff)
        response = self.client.post(
            reverse("group-settings-api", args=[-400]),
            data={
                "max_warnings_action": "mute",
                "max_warnings_action_delay_minutes": 15,
                "max_warnings_action_duration_minutes": 20,
                "message_templates": {"mute": "{target} ساکت شد"},
            },
            content_type="application/json",
            HTTP_X_API_KEY=raw,
        )
        assert response.status_code == 200
        self.group.refresh_from_db()
        assert self.group.max_warnings_action_delay_minutes == 15
        assert self.group.message_templates["mute"] == "{target} ساکت شد"

    def test_zero_duration_is_rejected_instead_of_crashing_later(self):
        _, raw = create_api_key("settings-write", self.staff)
        response = self.client.post(
            reverse("group-settings-api", args=[-400]),
            data={"max_warnings_action_duration_minutes": 0},
            content_type="application/json",
            HTTP_X_API_KEY=raw,
        )
        assert response.status_code == 400
        self.group.refresh_from_db()
        assert self.group.max_warnings_action_duration_minutes != 0

    def test_api_key_chat_scope_blocks_other_groups(self):
        key, raw = create_api_key("scoped", self.staff)
        key.allowed_chat_ids = [-400]
        key.save(update_fields=["allowed_chat_ids"])
        allowed = self.client.get(
            reverse("group-settings-api", args=[-400]),
            HTTP_X_API_KEY=raw,
        )
        denied = self.client.get(
            reverse("group-settings-api", args=[-401]),
            HTTP_X_API_KEY=raw,
        )
        assert allowed.status_code == 200
        assert denied.status_code == 403


class StaleProcessingRequeueTest(TestCase):
    def setUp(self):
        self.group = GroupSettings.objects.create(chat_id=-501)

    def test_side_effect_actions_fail_closed_release_actions_requeue(self):
        now = timezone.now()
        stale = now - timedelta(minutes=30)
        ban, _ = create_action(group=self.group, action="ban", target_user_id=9, source="telegram")
        unlock, _ = create_action(group=self.group, action="unlock", source="telegram")
        ModerationAction.objects.filter(pk__in=[ban.pk, unlock.pk]).update(
            status="processing",
            started_at=stale,
        )
        changed = requeue_stale_processing_actions(stale_minutes=10, now=now)
        ban.refresh_from_db()
        unlock.refresh_from_db()
        assert changed == 2
        assert ban.status == "failed"
        assert unlock.status == "pending"
        assert unlock.started_at is None


class ChatLinkTest(TestCase):
    def test_expired_or_inactive_link_is_invalid(self):
        link = ChatLink(expires_at=timezone.now() - timedelta(seconds=1))
        assert link.is_valid() is False

        link.expires_at = None
        link.is_active = False
        assert link.is_valid() is False


class AiApiTest(TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_noya_api_without_key_does_not_make_request(self):
        with patch("botapp.services.httpx.AsyncClient") as client:
            result = async_to_sync(call_noya_api)("سلام", "telegram:1", use_agent=False)

        assert result[0] == "خطا در ارتباط با نویا. لطفاً دوباره تلاش کنید."
        client.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "NOYA_API_KEY": "test-key",
            "NOYA_API_URL": "https://example.test/v1/chat/completions",
            "NOYA_MODEL": "test-model",
        },
        clear=True,
    )
    def test_noya_api_reads_credentials_and_model_from_environment(self):
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "پاسخ نویا"}}]},
            request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
        )
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("botapp.services.httpx.AsyncClient", return_value=context):
            result = async_to_sync(call_noya_api)("سلام", "telegram:1", use_agent=False)

        assert result[0] == "پاسخ نویا"
        kwargs = client.post.await_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["json"]["model"] == "test-model"
        assert kwargs["json"]["stream"] is False

    def test_successful_response_returns_content(self):
        response = httpx.Response(
            200,
            json={"content": "پاسخ"},
            request=httpx.Request("POST", "https://example.test/api"),
        )
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("botapp.services.httpx.AsyncClient", return_value=context):
            result = async_to_sync(call_ai_api)(
                "https://example.test/api",
                "سوال",
                "telegram:1",
            )

        assert result == "پاسخ"
        client.post.assert_awaited_once()

    def test_http_error_does_not_leak_upstream_details(self):
        response = httpx.Response(
            500,
            text="secret upstream details",
            request=httpx.Request("POST", "https://example.test/api"),
        )
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("botapp.services.httpx.AsyncClient", return_value=context):
            result = async_to_sync(call_ai_api)(
                "https://example.test/api",
                "سوال",
                "telegram:1",
            )

        assert result == "خطا در ارتباط با هوش مصنوعی. لطفا دوباره تلاش کنید."
        assert "secret" not in result

    def test_invalid_json_does_not_raise(self):
        response = httpx.Response(
            200,
            text="not-json",
            request=httpx.Request("POST", "https://example.test/api"),
        )
        client = AsyncMock()
        client.post.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = client

        with patch("botapp.services.httpx.AsyncClient", return_value=context):
            result = async_to_sync(call_ai_api)(
                "https://example.test/api",
                "سوال",
                "telegram:1",
            )

        assert result == "خطا در ارتباط با هوش مصنوعی. لطفا دوباره تلاش کنید."


class PanelCallbackTest(TestCase):
    def test_toggle_callback_refreshes_the_panel_message(self):
        from botapp.management.commands.runbot import handle_toggle_callback

        group = GroupSettings.objects.create(chat_id=-500, chat_title="panel", welcome_enabled=True)
        callback = AsyncMock()
        callback.from_user.id = 1
        callback.from_user.first_name = "Admin"
        callback.data = f"toggle:welcome:{group.chat_id}"
        callback.message = AsyncMock()

        with patch(
            "botapp.management.commands.runbot.is_group_admin",
            AsyncMock(return_value=True),
        ):
            async_to_sync(handle_toggle_callback)(callback, AsyncMock())

        group.refresh_from_db()
        assert group.welcome_enabled is False
        callback.message.edit_text.assert_called_once()


class HealthcheckTest(TestCase):
    def test_healthcheck(self):
        response = self.client.get(reverse("healthcheck"))
        assert response.status_code == 200
        assert response.content == b"ok"

    def test_healthcheck_behind_https_proxy_does_not_redirect(self):
        with self.settings(DEBUG=False, SECURE_SSL_REDIRECT=True):
            response = self.client.get(
                reverse("healthcheck"),
                HTTP_X_FORWARDED_PROTO="https",
            )

        assert response.status_code == 200
        assert response.content == b"ok"
