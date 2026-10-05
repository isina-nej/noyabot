"""Deterministic, low-cost Persian command parser.

This runs before any AI call. It recognises the common, unambiguous admin
phrasings and returns a fully-formed :class:`AgentDecision`. Only when it cannot
recognise an intent with confidence does the orchestrator fall back to the AI
structured parser.

The parser never resolves targets itself (that requires live context); it only
sets ``target_source`` so the orchestrator can resolve safely.
"""

from __future__ import annotations

import re

from .schemas import AgentDecision, ToolParameters

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_WORD_NUMBERS = {
    "نیم": 0.5,
    "یک": 1, "یه": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6, "شیش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
    "پانزده": 15, "پونزده": 15,
    "بیست": 20,
    "سی": 30,
    "چهل": 40,
    "پنجاه": 50,
    "شصت": 60,
}


def normalize_digits(text: str) -> str:
    return (text or "").translate(_DIGIT_MAP)


def _word_number(token: str) -> float | None:
    return _WORD_NUMBERS.get(token.strip())


def parse_duration_minutes(text: str) -> int | None:
    """Extract a duration in minutes from free Persian text.

    Supports ``۳۰ دقیقه``, ``2 ساعت``, ``نیم ساعت``, ``یک روز`` and bare numbers
    (interpreted as minutes). Returns ``None`` when no duration is present.
    """
    text = normalize_digits(text)
    # number (digits or a known word) followed by an optional unit
    pattern = re.compile(
        r"(?P<num>\d+(?:\.\d+)?|" + "|".join(map(re.escape, _WORD_NUMBERS)) + r")\s*"
        r"(?P<unit>دقیقه|دقیقه‌ای|min|m|ساعت|ساعته|hour|h|روز|روزه|day|d)?"
    )
    match = pattern.search(text)
    if not match:
        return None
    raw = match.group("num")
    value = float(raw) if re.fullmatch(r"\d+(?:\.\d+)?", raw) else _word_number(raw)
    if value is None:
        return None
    unit = (match.group("unit") or "").strip()
    if unit in {"ساعت", "ساعته", "hour", "h"}:
        value *= 60
    elif unit in {"روز", "روزه", "day", "d"}:
        value *= 1440
    minutes = int(round(value))
    return minutes if minutes >= 1 else None


def extract_int(text: str) -> int | None:
    text = normalize_digits(text)
    match = re.search(r"\d+", text)
    if match:
        return int(match.group())
    for token in text.split():
        value = _word_number(token)
        if value is not None and value == int(value):
            return int(value)
    return None


def _has_any(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


def _decision(tool, parameters, *, intent, summary, confidence=1.0):
    return AgentDecision(
        intent=intent,
        tool=tool,
        confidence=confidence,
        parameters=ToolParameters(**parameters),
        human_summary=summary,
    )


_ENABLE = ("فعال", "روشن", "بذار", "بگذار", "enable", "on")
_DISABLE = ("غیرفعال", "خاموش", "بردار", "disable", "off")


def is_message_analysis_request(text: str) -> bool:
    """Detect if the user is asking to analyze/inspect a specific message or text.

    E.g. prompt injection check, analyzing a replied text, photo caption, currency message.
    Must NOT be routed to generic group analytics (analytics.generate_briefing)
    and must NOT trigger image editing or media shortcuts.
    """
    t = " ".join((text or "").replace("\u200c", "").casefold().split())
    has_target = _has_any(
        t,
        "این متن",
        "این پیام",
        "این عکس",
        "این تصویر",
        "متن رو",
        "پیام رو",
        "عکس رو",
        "تصویر رو",
        "متن را",
        "پیام را",
        "عکس را",
        "تصویر را",
        "متن این",
        "پیام این",
        "عکس این",
        "تصویر این",
        "متن زیر",
        "پیام زیر",
        "متن بالا",
        "پیام بالا",
        "متن ریپلای",
        "پیام ریپلای",
        "کپشن",
        "کپشن رو",
        "کپشن این",
        "محتوای این",
    )
    has_injection = _has_any(
        t,
        "تزریق دستور",
        "تزریق پرامپت",
        "prompt injection",
        "تزریق کد",
        "تزریق",
        "injection",
        "پرامپت مخرب",
        "دستور مخرب",
    )
    has_inspection = _has_any(
        t,
        "تحلیل متن",
        "تحلیل پیام",
        "بررسی متن",
        "بررسی پیام",
        "آنالیز متن",
        "آنالیز پیام",
        "تحلیل عکس",
        "تحلیل تصویر",
        "بررسی عکس",
        "بررسی تصویر",
    )
    if has_injection or has_inspection:
        return True
    if has_target and _has_any(t, "تحلیل", "بررسی", "آنالیز", "چک", "تست", "ارزیابی", "inspect", "analyze", "نظر", "امنیت"):
        return True
    return False


def parse(command: str) -> AgentDecision | None:  # noqa: C901 - flat rule table
    raw = (command or "").strip()
    if not raw:
        return None
    # Remove ZWNJ (نیم‌فاصله) and collapse spaces so keyword matching is robust.
    text = " ".join(normalize_digits(raw).replace("\u200c", "").casefold().split())

    status_query = ("چطور", "وضعیت", "الان", "چیه", "ببین", "نشون")
    has_enable = _has_any(text, *_ENABLE)
    has_disable = _has_any(text, *_DISABLE)

    # --- Read-only: group info ------------------------------------------------
    if _has_any(text, "چند ادمین", "چندتا ادمین", "تعداد ادمین", "چند تا ادمین", "لیست ادمین", "ادمینها", "ادمین ها"):
        return _decision("group.get_admins", {}, intent="get_admins", summary="نمایش ادمین‌ها")
    if _has_any(text, "تعداد اعضا", "چند عضو", "چندتا عضو", "چند تا عضو", "چند نفر", "اعضای گروه", "چند عضوی"):
        return _decision("group.get_member_count", {}, intent="get_member_count", summary="نمایش تعداد اعضا")
    if "دسترسی" in text and _has_any(text, "خودت", "ربات", "هات", "بات", "پرمیشن"):
        return _decision("group.get_bot_permissions", {}, intent="get_bot_permissions", summary="نمایش دسترسی‌های ربات")
    if (
        _has_any(text, "ضداسپم", "ضد اسپم", "ضدلینک", "ضد لینک", "ضدفوروارد", "مدیریت")
        and _has_any(text, *status_query)
        and not (has_enable or has_disable)
    ):
        return _decision("group.get_moderation_status", {}, intent="get_moderation_status", summary="نمایش وضعیت مدیریت")
    if "تنظیمات" in text:
        return _decision("group.get_settings", {}, intent="get_settings", summary="نمایش تنظیمات گروه")

    # --- Read-only: analytics / audit ----------------------------------------
    # Message volume — prefer before generic «آمار امروز» so «امروز چند پیام» lands here.
    if _has_any(
        text,
        "چند پیام",
        "چندتا پیام",
        "چند تا پیام",
        "تعداد پیام",
        "آمار پیام",
        "پیام داد",
        "پیام داشته",
        "پیام امروز",
        "فعالیت پیام",
        "چند نفر پیام",
    ) or (
        _has_any(text, "پیام") and _has_any(text, "چند", "تعداد", "امروز", "بشمر", "بشمار", "شمارش")
    ):
        if _has_any(text, "هفته", "۷ روز", "7 روز", "هفت روز"):
            return _decision(
                "analytics.get_message_activity_period",
                {},
                intent="message_activity_period",
                summary="آمار پیام ۷ روز",
            )
        return _decision(
            "analytics.get_message_activity_today",
            {},
            intent="message_activity_today",
            summary="تعداد پیام امروز",
        )
    if _has_any(text, "فعال‌ترین", "فعال ترین", "بیشترین پیام", "تاپ ارسال", "پرکارترین"):
        return _decision(
            "analytics.get_top_senders_today",
            {},
            intent="top_senders",
            summary="فعال‌ترین ارسال‌کنندگان امروز",
        )
    if _has_any(text, "آمار امروز", "آمار فعالیت امروز", "فعالیت امروز", "آمار روز"):
        return _decision("analytics.get_today_summary", {}, intent="today_summary", summary="آمار امروز")
    if _has_any(text, "آمار هفته", "آمار هفتگی", "آمار ۷ روز", "آمار 7 روز", "گزارش هفته", "آمار هفت روز"):
        return _decision("analytics.get_period_summary", {}, intent="period_summary", summary="آمار ۷ روز")
    # Deep / multi-step investigation (ReAct harness) before simple briefing.
    if _has_any(
        text,
        "تحلیل عمیق",
        "بررسی کامل",
        "بررسی مرحله",
        "تحقیق کامل",
        "با ابزار بررسی",
        "مرحله به مرحله",
        "deep analysis",
        "investigate",
    ) or (
        _has_any(text, "بررسی") and _has_any(text, "کامل", "عمیق", "دقیق", "همه‌جانبه", "جامع")
    ) or (
        _has_any(text, "بررسی") and _has_any(text, "گروه", "کانال") and not is_message_analysis_request(text)
    ) or (
        _has_any(text, "تحقیق") and _has_any(text, "گروه", "کانال")
    ):
        return _decision(
            "harness.investigate",
            {},
            intent="harness_investigate",
            summary="بررسی مرحله‌ای گروه/کانال",
        )
    if is_message_analysis_request(text):
        return None

    if _has_any(text, "تحلیل", "آنالیز", "briefing", "گزارش تحلیلی", "وضعیت کلی"):
        # «تحلیل … چند پیام» already matched above; pure analysis → briefing.
        return _decision("analytics.generate_briefing", {}, intent="briefing", summary="تحلیل گروه/کانال")
    if _has_any(text, "بیشترین تخلف", "بیشترین اخطار", "کاربران پرریسک", "تاپ متخلف", "بیشترین اقدام"):
        return _decision("analytics.get_top_moderated_users", {}, intent="top_moderated", summary="کاربران پرتکرار مدیریتی")
    if "حذف" in text and _has_any(text, "ربات", "بات") and _has_any(text, "آخرین", "کرده", "چی", "کدوم", "لیست"):
        return _decision("message.get_bot_deleted_recent", {}, intent="bot_deleted_recent", summary="آخرین پیام‌های حذف‌شده توسط ربات")
    if _has_any(text, "آخرین عملیات", "آخرین اقدام", "تاریخچه عملیات", "گزارش عملیات", "لاگ عملیات", "آخرین عملیات مدیریتی"):
        return _decision("audit.get_recent_actions", {}, intent="recent_actions", summary="آخرین عملیات مدیریتی")

    # --- Channel-specific reads ----------------------------------------------
    if _has_any(text, "اطلاعات کانال", "مشخصات کانال", "درباره کانال"):
        return _decision("channel.get_info", {}, intent="channel_info", summary="اطلاعات کانال")
    if _has_any(text, "تعداد مشترک", "چند مشترک", "چندتا مشترک", "سابسکرایبر", "subscriber"):
        return _decision("channel.get_subscriber_count", {}, intent="channel_subscribers", summary="تعداد مشترکین کانال")
    if _has_any(text, "ادمین کانال", "ادمین‌های کانال", "ادمینهای کانال"):
        return _decision("channel.get_admins", {}, intent="channel_admins", summary="ادمین‌های کانال")

    # --- Member warnings (read) ----------------------------------------------
    if _has_any(text, "اخطارها", "اخطار های", "چند اخطار", "تعداد اخطار") and not _has_any(text, "بده", "بزن", "حداکثر", "سقف"):
        return _decision("member.get_warnings", {"target_source": "reply"}, intent="get_warnings", summary="نمایش اخطارهای کاربر")

    # --- Settings write: toggles ---------------------------------------------
    for feature, enable_tool, disable_tool in (
        (("ضدلینک", "ضد لینک", "anti link", "anti-link"), "settings.enable_anti_link", "settings.disable_anti_link"),
        (("ضداسپم", "ضد اسپم", "anti spam"), "settings.enable_anti_spam", "settings.disable_anti_spam"),
        (("ضدفوروارد", "ضد فوروارد", "anti forward"), "settings.enable_anti_forward", "settings.disable_anti_forward"),
    ):
        if _has_any(text, *feature):
            if _has_any(text, *_DISABLE):
                return _decision(disable_tool, {}, intent="settings_toggle", summary="غیرفعال‌کردن تنظیمات")
            if _has_any(text, *_ENABLE):
                return _decision(enable_tool, {}, intent="settings_toggle", summary="فعال‌کردن تنظیمات")

    # --- Settings write: numeric ---------------------------------------------
    if _has_any(text, "حداکثر اخطار", "سقف اخطار", "max warning"):
        value = extract_int(text)
        if value is not None:
            return _decision("settings.set_max_warnings", {"value": value}, intent="set_max_warnings", summary="تنظیم سقف اخطار")
    if _has_any(text, "محدودیت پیام", "سقف پیام", "flood", "فلاد"):
        value = extract_int(text)
        if value is not None:
            return _decision("settings.set_flood_limit", {"value": value}, intent="set_flood_limit", summary="تنظیم محدودیت پیام")

    # --- Blocked words / allowed domains -------------------------------------
    word_match = re.search(r"کلمه(?:\s*ممنوعه?)?\s+(?P<w>\S+)", raw)
    if _has_any(text, "کلمه ممنوع", "فیلتر کلمه") and word_match:
        word = word_match.group("w")
        if _has_any(text, "حذف", "بردار", "پاک"):
            return _decision("settings.remove_blocked_word", {"value": word}, intent="remove_blocked_word", summary="حذف کلمه ممنوع")
        return _decision("settings.add_blocked_word", {"value": word}, intent="add_blocked_word", summary="افزودن کلمه ممنوع")

    # --- Messages -------------------------------------------------------------
    if _has_any(text, "این پیام رو پین", "این پیام را پین", "پین کن", "سنجاق") and "آن" not in text and "unpin" not in text:
        return _decision("message.pin", {"target_source": "reply"}, intent="pin_message", summary="پین‌کردن پیام")
    if _has_any(text, "آن‌پین", "آنپین", "برداشتن پین", "رفع پین", "unpin"):
        return _decision("message.unpin", {"target_source": "reply"}, intent="unpin_message", summary="برداشتن پین پیام")
    if _has_any(text, "این پیام رو حذف", "این پیام را حذف", "پیام رو پاک", "پیام را پاک") or (
        text.strip() in {"حذف", "این رو حذف کن", "پاکش کن", "delete", "del"}
    ):
        return _decision("message.delete", {"target_source": "reply"}, intent="delete_message", summary="حذف پیام")

    if _has_any(text, "زمان‌بندی", "زمانبندی", "تایمر روزانه", "برنامه روزانه"):
        if _has_any(text, "حذف", "بردار", "لغو"):
            action = "unlock" if _has_any(text, "باز") else "lock"
            clock = re.search(r"(\d{1,2}[:.]\d{2})", text)
            if clock:
                return _decision(
                    "group.remove_daily_schedule",
                    {"action": action, "value": clock.group(1)},
                    intent="remove_schedule",
                    summary="حذف زمان‌بندی روزانه",
                )
        if _has_any(text, "بذار", "اضافه", "ثبت", "ست کن", "هر روز"):
            action = "unlock" if _has_any(text, "باز") else "lock"
            clock = re.search(r"(\d{1,2}[:.]\d{2})", text)
            if clock:
                return _decision(
                    "group.add_daily_schedule",
                    {"action": action, "value": clock.group(1)},
                    intent="add_schedule",
                    summary="ثبت زمان‌بندی روزانه",
                )
        return _decision("group.get_schedules", {}, intent="get_schedules", summary="نمایش زمان‌بندی‌ها")

    # --- Group lock / unlock --------------------------------------------------
    if _has_any(text, "قفل") and _has_any(text, "گروه", "چت") and not _has_any(text, "باز", "رفع", "unlock"):
        duration = parse_duration_minutes(text)
        params = {"duration_minutes": duration} if duration else {}
        return _decision("group.lock", params, intent="lock_group", summary="قفل گروه")
    if (_has_any(text, "باز کن", "بازکن", "رفع قفل", "unlock") and _has_any(text, "گروه", "قفل", "چت")):
        return _decision("group.unlock", {}, intent="unlock_group", summary="بازکردن گروه")

    # --- Member moderation ----------------------------------------------------
    if _has_any(text, "رفع سکوت", "رفع میوت", "آنمیوت", "unmute", "بردار محدودیت", "محدودیت.*بردار") or (
        _has_any(text, "محدودیت") and _has_any(text, "بردار", "رفع")
    ):
        return _decision("member.unmute", {"target_source": "reply"}, intent="unmute", summary="رفع محدودیت کاربر")
    if _has_any(text, "ساکت", "سکوت", "میوت", "mute") and not _has_any(text, "رفع", "آن", "un"):
        duration = parse_duration_minutes(text)
        params = {"target_source": "reply", "reason": _extract_reason(raw)}
        if duration:
            params["duration_minutes"] = duration
        return _decision("member.mute", params, intent="mute", summary="محدودکردن کاربر")
    if _has_any(text, "رفع بن", "آنبن", "unban") or (_has_any(text, "بن") and _has_any(text, "بردار", "رفع")):
        return _decision("member.unban", {"target_source": "reply"}, intent="unban", summary="رفع بن کاربر")
    if _has_any(text, "بن کن", "بن‌کن", "ban", "مسدود", "بلاک") and not _has_any(text, "رفع", "آن", "un"):
        return _decision(
            "member.ban",
            {"target_source": "reply", "reason": _extract_reason(raw)},
            intent="ban",
            summary="بن کاربر",
        )
    if _has_any(text, "اخطار") and _has_any(text, "بده", "بزن", "کن"):
        return _decision(
            "member.warn",
            {"target_source": "reply", "reason": _extract_reason(raw)},
            intent="warn",
            summary="اخطار به کاربر",
        )

    return None


_REASON_MARKERS = ("چون", "به‌خاطر", "به خاطر", "بخاطر", "برای", "دلیل")


def _extract_reason(raw: str) -> str:
    for marker in _REASON_MARKERS:
        idx = raw.find(marker)
        if idx != -1:
            return raw[idx + len(marker):].strip()[:500]
    return ""
