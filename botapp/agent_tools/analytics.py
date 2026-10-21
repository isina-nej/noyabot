"""Analytics tools based on data actually recorded by the project."""

from __future__ import annotations

import logging
import os
from datetime import timedelta

import httpx
from asgiref.sync import sync_to_async
from django.db.models import Count
from django.utils import timezone

from botapp.agent.context import AgentContext
from botapp.agent.permissions import ANALYTICS_READ
from botapp.agent.registry import register_tool
from botapp.agent.responses import fa_number
from botapp.agent.risk import LOW
from botapp.agent.schemas import EmptyParams

logger = logging.getLogger("botapp.agent")

_ACTION_LABELS = {
    "warn": "اخطار",
    "mute": "سکوت",
    "unmute": "رفع سکوت",
    "ban": "بن",
    "unban": "رفع بن",
    "lock": "قفل",
    "unlock": "بازکردن",
    "delete": "حذف",
    "filter": "فیلتر",
}


@sync_to_async(thread_sensitive=True)
def _summary(group_id: int, since):
    from botapp.models import ModerationLog

    rows = (
        ModerationLog.objects.filter(group_id=group_id, created_at__gte=since)
        .values("action")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    return [(row["action"], row["total"]) for row in rows]


@sync_to_async(thread_sensitive=True)
def _top_targets(group_id: int, since, limit: int = 5):
    from botapp.models import ModerationLog

    rows = (
        ModerationLog.objects.filter(
            group_id=group_id,
            created_at__gte=since,
            target_user_id__isnull=False,
        )
        .exclude(action__in={"unlock", "unmute", "unban"})
        .values("target_user_id", "target_name")
        .annotate(total=Count("id"))
        .order_by("-total")[:limit]
    )
    return list(rows)


@sync_to_async(thread_sensitive=True)
def _forced_membership_snapshot(chat_id: int):
    from botapp.models import ForcedMembershipRule, ForcedMembershipUserState

    rules = list(
        ForcedMembershipRule.objects.filter(source_group__chat_id=chat_id, is_active=True)
    )
    if not rules:
        return None
    out = []
    for rule in rules:
        states = ForcedMembershipUserState.objects.filter(rule=rule)
        out.append(
            {
                "destination": rule.destination_chat_title or rule.destination_chat_id,
                "tracked": states.count(),
                "members": states.filter(is_currently_member=True).count(),
                "never_joined": states.filter(has_ever_joined=False).count(),
            }
        )
    return out


async def _render_summary(ctx: AgentContext, since, header: str) -> str:
    if ctx.group_settings is None:
        return "داده‌ای برای این چت ثبت نشده است."
    rows = await _summary(ctx.group_settings.id, since)
    if not rows:
        return f"{header}\nهیچ عملیات مدیریتی ثبت نشده است."
    total = sum(count for _, count in rows)
    lines = [f"• {_ACTION_LABELS.get(action, action)}: {fa_number(count)}" for action, count in rows]
    return f"{header}\nمجموع عملیات: {fa_number(total)}\n" + "\n".join(lines)


async def today_summary(ctx: AgentContext, bot, params) -> str:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return await _render_summary(ctx, start, "📊 آمار عملیات مدیریتی امروز:")


async def period_summary(ctx: AgentContext, bot, params) -> str:
    since = timezone.now() - timedelta(days=7)
    return await _render_summary(ctx, since, "📊 آمار عملیات مدیریتی ۷ روز اخیر:")


async def top_moderated_users(ctx: AgentContext, bot, params) -> str:
    if ctx.group_settings is None:
        return "داده‌ای برای این چت ثبت نشده است."
    since = timezone.now() - timedelta(days=7)
    rows = await _top_targets(ctx.group_settings.id, since)
    if not rows:
        return "📊 در ۷ روز اخیر کاربر هدف‌داری در لاگ مدیریتی ثبت نشده است."
    lines = []
    for index, row in enumerate(rows, start=1):
        name = (row.get("target_name") or "").strip() or str(row["target_user_id"])
        lines.append(f"{fa_number(index)}. {name} — {fa_number(row['total'])} اقدام")
    return "📊 کاربران با بیشترین اقدام مدیریتی (۷ روز):\n" + "\n".join(lines)


@sync_to_async(thread_sensitive=True)
def _activity_today(chat_id: int):
    from botapp import activity as activity_svc

    return activity_svc.get_activity(chat_id)


@sync_to_async(thread_sensitive=True)
def _activity_range(chat_id: int, days: int = 7):
    from botapp import activity as activity_svc

    until = timezone.localdate()
    since = until - timedelta(days=max(days, 1) - 1)
    return activity_svc.get_activity_range(chat_id, since, until)


@sync_to_async(thread_sensitive=True)
def _top_senders_today(chat_id: int, limit: int = 10):
    from botapp import activity as activity_svc

    return activity_svc.get_top_senders(chat_id, limit=limit)


async def message_activity_today(ctx: AgentContext, bot, params) -> str:
    row = await _activity_today(ctx.chat_id)
    today = timezone.localdate()
    if row is None or row.message_count == 0:
        return (
            f"📨 پیام‌های مشاهده‌شدهٔ امروز ({today.isoformat()}) در این {ctx.label()}: "
            f"{fa_number(0)}\n\n"
            "نکته: شمارش از زمانی شروع می‌شود که ربات در چت باشد و قابلیت "
            "شمارش فعالیت فعال شده باشد. تاریخچهٔ قبل از آن از Bot API قابل "
            "بازخوانی نیست."
        )
    return (
        f"📨 آمار پیام امروز ({today.isoformat()}) — {ctx.label()}:\n"
        f"• کل پیام‌ها: {fa_number(row.message_count)}\n"
        f"• رسانه (عکس/ویدیو/…): {fa_number(row.media_count)}\n"
        f"• ارسال‌کننده یکتا: {fa_number(row.unique_sender_count)}\n\n"
        "این اعداد از پیام‌هایی است که خودِ ربات در تلگرام دیده است."
    )


async def message_activity_period(ctx: AgentContext, bot, params) -> str:
    rows = await _activity_range(ctx.chat_id, 7)
    if not rows:
        return (
            f"📨 در ۷ روز اخیر هنوز پیامی برای این {ctx.label()} شمارش نشده است.\n"
            "شمارش فقط از زمان فعال‌سازی ربات/قابلیت انجام می‌شود."
        )
    total = sum(r.message_count for r in rows)
    media = sum(r.media_count for r in rows)
    lines = [
        f"• {r.day.isoformat()}: {fa_number(r.message_count)} پیام"
        f" / {fa_number(r.unique_sender_count)} نفر"
        for r in rows
    ]
    return (
        f"📨 آمار پیام ۷ روز اخیر — {ctx.label()}:\n"
        f"مجموع: {fa_number(total)} پیام، رسانه: {fa_number(media)}\n\n"
        + "\n".join(lines)
    )


async def top_senders_today(ctx: AgentContext, bot, params) -> str:
    rows = await _top_senders_today(ctx.chat_id)
    if not rows:
        return "📨 امروز هنوز ارسال‌کننده‌ای شمارش نشده است."
    lines = []
    for index, row in enumerate(rows, start=1):
        name = (row.display_name or "").strip() or (
            f"@{row.username}" if row.username else str(row.user_id)
        )
        lines.append(f"{fa_number(index)}. {name} — {fa_number(row.message_count)} پیام")
    return "📨 فعال‌ترین ارسال‌کنندگان امروز:\n" + "\n".join(lines)


async def _collect_facts(ctx: AgentContext, bot) -> dict:
    facts: dict = {
        "chat_id": ctx.chat_id,
        "chat_title": ctx.chat_title,
        "chat_kind": "channel" if ctx.is_channel else "group",
        "admin_role": ctx.admin.role,
    }
    try:
        chat = await bot.get_chat(ctx.chat_id)
        facts["title"] = getattr(chat, "title", "") or ctx.chat_title
        facts["username"] = getattr(chat, "username", "") or ""
        facts["type"] = getattr(chat, "type", ctx.chat_type)
        facts["member_count"] = await bot.get_chat_member_count(ctx.chat_id)
        admins = await bot.get_chat_administrators(ctx.chat_id)
        facts["admin_count"] = len(admins)
    except Exception as exc:  # noqa: BLE001 - briefing must degrade gracefully
        facts["telegram_error"] = type(exc).__name__

    if ctx.group_settings is not None:
        group = ctx.group_settings
        facts["settings"] = {
            "moderation_enabled": group.moderation_enabled,
            "anti_spam_enabled": group.anti_spam_enabled,
            "anti_link_enabled": group.anti_link_enabled,
            "anti_forward_enabled": group.anti_forward_enabled,
            "max_warnings": group.max_warnings,
            "flood_limit": group.flood_limit,
            "blocked_words": len(group.blocked_words or []),
            "allowed_domains": len(group.allowed_domains or []),
        }
        since = timezone.now() - timedelta(days=7)
        facts["moderation_7d"] = await _summary(group.id, since)
        facts["top_targets_7d"] = [
            {
                "name": (row.get("target_name") or "").strip() or str(row["target_user_id"]),
                "count": row["total"],
            }
            for row in await _top_targets(group.id, since)
        ]
        forced = await _forced_membership_snapshot(ctx.chat_id)
        if forced:
            facts["forced_membership"] = forced

    today = await _activity_today(ctx.chat_id)
    week = await _activity_range(ctx.chat_id, 7)
    facts["messages_today"] = {
        "count": int(today.message_count) if today else 0,
        "media": int(today.media_count) if today else 0,
        "unique_senders": int(today.unique_sender_count) if today else 0,
    }
    facts["messages_7d"] = {
        "count": sum(int(r.message_count) for r in week),
        "media": sum(int(r.media_count) for r in week),
        "days_with_data": len(week),
    }
    top_senders = await _top_senders_today(ctx.chat_id, 5)
    facts["top_senders_today"] = [
        {
            "name": (s.display_name or s.username or str(s.user_id)),
            "count": int(s.message_count),
        }
        for s in top_senders
    ]
    return facts


def _facts_as_text(facts: dict) -> str:
    lines = [
        f"نوع: {facts.get('chat_kind')}",
        f"عنوان: {facts.get('title') or facts.get('chat_title')}",
        f"شناسه: {facts.get('chat_id')}",
    ]
    if facts.get("username"):
        lines.append(f"یوزرنیم: @{facts['username']}")
    if "member_count" in facts:
        lines.append(f"اعضا/مشترکین: {facts['member_count']}")
    if "admin_count" in facts:
        lines.append(f"تعداد ادمین: {facts['admin_count']}")
    msg_today = facts.get("messages_today") or {}
    lines.append(
        "پیام امروز: "
        f"{msg_today.get('count', 0)} "
        f"(رسانه={msg_today.get('media', 0)}, "
        f"ارسال‌کننده یکتا={msg_today.get('unique_senders', 0)})"
    )
    msg_week = facts.get("messages_7d") or {}
    lines.append(
        "پیام ۷روز: "
        f"{msg_week.get('count', 0)} "
        f"(روزهای دارای داده={msg_week.get('days_with_data', 0)})"
    )
    senders = facts.get("top_senders_today") or []
    if senders:
        lines.append(
            "فعال‌ترین‌های امروز: "
            + ", ".join(f"{row['name']}({row['count']})" for row in senders)
        )
    settings = facts.get("settings") or {}
    if settings:
        lines.append(
            "تنظیمات: "
            + ", ".join(
                f"{key}={'on' if value else 'off'}" if isinstance(value, bool) else f"{key}={value}"
                for key, value in settings.items()
            )
        )
    mod = facts.get("moderation_7d") or []
    if mod:
        lines.append(
            "عملیات ۷روز: "
            + ", ".join(f"{_ACTION_LABELS.get(a, a)}={c}" for a, c in mod)
        )
    else:
        lines.append("عملیات ۷روز: هیچ")
    tops = facts.get("top_targets_7d") or []
    if tops:
        lines.append(
            "بیشترین هدف‌ها: "
            + ", ".join(f"{row['name']}({row['count']})" for row in tops)
        )
    forced = facts.get("forced_membership") or []
    for rule in forced:
        lines.append(
            "عضویت اجباری → "
            f"{rule['destination']}: tracked={rule['tracked']}, "
            f"members={rule['members']}, never_joined={rule['never_joined']}"
        )
    return "\n".join(lines)


async def _ai_narrative(facts_text: str) -> str | None:
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    if not api_key:
        return None
    url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    model = os.getenv("AGENT_MODEL", "").strip() or os.getenv("NOYA_MODEL", "TinkeraBot")
    system = (
        "تو تحلیل‌گر داخلی ربات مدیریت تلگرام هستی و به آمار واقعی دسترسی داری "
        "(داخل <facts>). گزارش کوتاه فارسی بنویس (حداکثر ۸ خط): وضعیت، ریسک، پیشنهاد. "
        "هرگز نگو به دیتابیس/API دسترسی نداری و هرگز فایل Export یا اسکریپت پایتون طلب نکن. "
        "فقط از <facts> استفاده کن."
    )
    user = (
        "بر اساس این حقایق یک تحلیل مدیریتی بنویس:\n"
        f"<facts>\n{facts_text}\n</facts>"
    )
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                },
            )
            response.raise_for_status()
            text = (response.text or "").strip()
            if not text:
                logger.warning("analytics briefing AI returned empty body (status=%s)", response.status_code)
                return None
            try:
                data = response.json()
            except Exception as json_err:
                logger.warning(
                    "analytics briefing AI returned non-JSON (status=%s): %s",
                    response.status_code,
                    type(json_err).__name__,
                )
                return None
            content = data["choices"][0]["message"]["content"]
            # Same outbound gate as the Noya chat path: model prose must never
            # carry internal planning annotations into an admin chat.
            from botapp.agent.responses import sanitize_outgoing_text

            return sanitize_outgoing_text(content or "") or None
    except httpx.ReadTimeout:
        logger.warning("analytics briefing AI call timed out (ReadTimeout) after 45s")
        return None
    except httpx.TimeoutException:
        logger.warning("analytics briefing AI call timed out")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("analytics briefing AI call failed: %s", type(exc).__name__)
        return None


async def generate_briefing(ctx: AgentContext, bot, params) -> str:
    facts = await _collect_facts(ctx, bot)
    facts_text = _facts_as_text(facts)
    narrative = await _ai_narrative(facts_text)
    header = f"🧠 تحلیل {ctx.label()}: {facts.get('title') or ctx.chat_title or ctx.chat_id}"
    msg_today = facts.get("messages_today") or {}
    lead = (
        f"📨 امروز {fa_number(msg_today.get('count', 0))} پیام "
        f"از {fa_number(msg_today.get('unique_senders', 0))} نفر دیده شد."
    )
    body = narrative or "خلاصهٔ آماری آماده است (مدل متنی در دسترس نبود)."
    return f"{header}\n\n{lead}\n\n{body}\n\n——\n📊 داده‌های مبنا:\n{facts_text}"


register_tool(
    name="analytics.get_today_summary",
    description="خلاصه عملیات مدیریتی امروز.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=today_summary,
)
register_tool(
    name="analytics.get_period_summary",
    description="خلاصه عملیات مدیریتی ۷ روز اخیر.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=period_summary,
)
register_tool(
    name="analytics.get_top_moderated_users",
    description="کاربرانی که در ۷ روز اخیر بیشترین اقدام مدیریتی رویشان انجام شده.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=top_moderated_users,
)
register_tool(
    name="analytics.get_message_activity_today",
    description="تعداد پیام/رسانه/ارسال‌کننده یکتای امروز که ربات در این چت دیده است.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=message_activity_today,
    human_verb="شمارش پیام امروز",
)
register_tool(
    name="analytics.get_message_activity_period",
    description="آمار پیام ۷ روز اخیر بر اساس شمارندهٔ فعالیت ربات.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=message_activity_period,
    human_verb="آمار پیام هفته",
)
register_tool(
    name="analytics.get_top_senders_today",
    description="فعال‌ترین ارسال‌کنندگان پیام امروز.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=top_senders_today,
    human_verb="فعال‌ترین‌های امروز",
)
register_tool(
    name="analytics.generate_briefing",
    description="تحلیل مدیریتی گروه/کانال با ترکیب آمار پیام واقعی، لاگ مدیریتی و خلاصه هوش مصنوعی.",
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=generate_briefing,
    human_verb="تحلیل گروه/کانال",
)
