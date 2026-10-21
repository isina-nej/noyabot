"""Tests for live-search freshness: date parsing, recency ranking, honest blocks.

The defect these tests pin down is behavioural, not cosmetic: Noya used to
answer "what is the latest news?" from results whose publication dates were
never parsed, never shown and never ranked, so months-old articles were
presented as current and the model could not separate the SOURCE date from
the EVENT date.

No test here touches the network. Every clock is injected, so assertions are
deterministic.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from botapp.noya_search import _format_block
from botapp.web.dates import (
    age_days,
    format_source_date,
    parse_iso_date,
    parse_published_date,
)
from botapp.web.models import SearchResult
from botapp.web.policy import freshness_intent
from botapp.web.ranking import SearchResultRanker, recency_summary

# A fixed reference instant: 2026-10-20T12:00:00Z.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _days_ago(n: int) -> str:
    """ISO date ``n`` days before NOW."""
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d")


def _result(title: str, url: str, snippet: str = "", published_at: str = "") -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, published_at=published_at)


# ── 1. date parsing ──


class TestPublishedDateParsing(SimpleTestCase):
    """Absolute, relative and Persian-digit forms — and honest ``None``."""

    def test_absolute_iso_forms(self):
        midnight = datetime(2025, 3, 11, tzinfo=timezone.utc)
        for raw in ("2025-03-11", "2025/03/11"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_published_date(raw), midnight)
                self.assertEqual(parse_iso_date(raw), midnight)

        stamped = datetime(2025, 3, 11, 8, 0, tzinfo=timezone.utc)
        for raw in ("2025-03-11T08:00:00+00:00", "2025-03-11T08:00:00Z"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_published_date(raw), stamped)
                self.assertEqual(parse_iso_date(raw), stamped)

    def test_absolute_named_month_forms(self):
        expected = datetime(2025, 3, 11, tzinfo=timezone.utc)
        for raw in ("11 March 2025", "Mar 11, 2025", "11 Mar 2025"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_published_date(raw), expected)

    def test_persian_digit_forms(self):
        self.assertEqual(
            parse_published_date("۱۱ مارس ۲۰۲۵"),
            datetime(2025, 3, 11, tzinfo=timezone.utc),
        )

    def test_relative_english_forms_need_now(self):
        cases = {
            "2 days ago": NOW - timedelta(days=2),
            "3 hours ago": NOW - timedelta(hours=3),
            "yesterday": NOW - timedelta(days=1),
            "today": NOW,
            "last week": NOW - timedelta(days=7),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_published_date(raw, now=NOW), expected)
                # Relative dates are meaningless without a reference point.
                self.assertIsNone(parse_published_date(raw))

    def test_relative_persian_forms_need_now(self):
        cases = {
            "۲ روز پیش": NOW - timedelta(days=2),
            "۳ ساعت پیش": NOW - timedelta(hours=3),
            "دیروز": NOW - timedelta(days=1),
            "امروز": NOW,
            "هفته پیش": NOW - timedelta(days=7),
            "ماه پیش": NOW - timedelta(days=30),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_published_date(raw, now=NOW), expected)
                self.assertIsNone(parse_published_date(raw))

    def test_unknown_and_ambiguous_return_none(self):
        for raw in (
            "",
            "no date in this snippet",
            "Django 5.2 is out!",
            # A Jalali month name must never be guessed into a Gregorian date.
            "فروردین ۱۴۰۴",
            # A price sentence is not a publication date, even though it
            # contains the word «امروز».
            "قیمت دلار امروز ۱۰۴,۵۰۰ تومان",
            "31 April 2025",  # impossible calendar date
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_published_date(raw, now=NOW))

    def test_results_are_timezone_aware_utc(self):
        parsed = parse_published_date("2025-03-11")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_age_days_and_format_source_date(self):
        published = NOW - timedelta(days=40)
        self.assertEqual(age_days(published, now=NOW), 40)
        self.assertEqual(age_days(_days_ago(40), now=NOW), 40)
        self.assertIsNone(age_days("", now=NOW))
        self.assertIsNone(age_days(None, now=NOW))
        self.assertEqual(format_source_date(published), "2026-09-10")
        self.assertEqual(format_source_date("2026-09-10T23:00:00Z"), "2026-09-10")
        self.assertEqual(format_source_date("garbage"), "")
        self.assertEqual(format_source_date(""), "")


# ── 2. recency-aware ranking ──


class TestFreshnessRanking(SimpleTestCase):
    """A 3-year-old authoritative page must never outrank a 2-day-old source."""

    def _pool(self) -> list[SearchResult]:
        return [
            # Position 0 + authoritative domain: wins on relevance/authority.
            _result(
                "Python Official",
                "https://docs.python.org/3/whatsnew",
                "python release notes",
                _days_ago(1095),
            ),
            _result(
                "Undated Blog",
                "https://random-blog.xyz/undated",
                "python release notes",
            ),
            _result(
                "Fresh Currency",
                "https://random-blog.xyz/fresh",
                "python release notes",
                _days_ago(2),
            ),
            _result(
                "One Day Old",
                "https://random-blog.xyz/oneday",
                "python release notes",
                _days_ago(1),
            ),
        ]

    def test_latest_prefers_recent_over_authoritative_old(self):
        ranked = SearchResultRanker.rank(
            self._pool(), "python release notes", freshness="latest", now=NOW,
        )
        titles = [r.title for r in ranked]
        # Both in-window sources occupy the top two slots, in relevance order.
        self.assertEqual(set(titles[:2]), {"One Day Old", "Fresh Currency"})
        # The 3-year-old authority page is dead last, despite the authority
        # boost that would put it first on `general`.
        self.assertEqual(titles[-1], "Python Official")
        self.assertGreater(ranked[-1].age_days, 180)

    def test_unknown_never_outranks_known_fresh(self):
        ranked = SearchResultRanker.rank(
            self._pool(), "python release notes", freshness="latest", now=NOW,
        )
        titles = [r.title for r in ranked]
        self.assertLess(
            titles.index("One Day Old"), titles.index("Undated Blog"),
            "an undated result outranked a one-day-old result",
        )

    def test_known_old_never_outranks_known_recent(self):
        # >180 days old vs. inside the window, with authority reversed.
        ancient_authority = _result(
            "Ancient Authority", "https://docs.djangoproject.com/old",
            "django changelog", _days_ago(730),
        )
        recent_low_authority = _result(
            "Recent Blog", "https://random-blog.xyz/new",
            "django changelog", _days_ago(3),
        )
        ranked = SearchResultRanker.rank(
            [ancient_authority, recent_low_authority],
            "django changelog", freshness="latest", now=NOW,
        )
        self.assertEqual(ranked[0].title, "Recent Blog")
        self.assertLess(ranked[0].age_days, ranked[1].age_days)

    def test_general_is_unchanged_by_recency(self):
        """``general`` must stay the historical authority/relevance ordering."""
        ranked = SearchResultRanker.rank(
            self._pool(), "python release notes", freshness="general", now=NOW,
        )
        # The exact historical ordering: authority/position/keywords only, so
        # the 3-year-old docs.python.org page still wins and undated vs. dated
        # results are treated as equal peers.
        self.assertEqual(
            [r.title for r in ranked],
            ["Python Official", "Undated Blog", "Fresh Currency", "One Day Old"],
            "general freshness must not apply any recency boost or penalty",
        )
        self.assertGreater(ranked[0].age_days, 1000)

    def test_general_ignores_dates_even_when_present(self):
        old_authority = _result(
            "Old Authority", "https://docs.python.org/3/x", "python", _days_ago(900),
        )
        new_low = _result("New Low", "https://random-blog.xyz/n", "python", _days_ago(0))
        general = SearchResultRanker.rank(
            [old_authority, new_low], "python", freshness="general", now=NOW,
        )
        self.assertEqual([r.title for r in general], ["Old Authority", "New Low"])
        self.assertEqual(general[0].age_days, 900)  # age is still measured

    def test_ranking_is_deterministic_on_ties(self):
        a = _result("A", "https://a.example/1", "same", _days_ago(1))
        b = _result("B", "https://b.example/1", "same", _days_ago(1))
        first = SearchResultRanker.rank([a, b], "", freshness="latest", now=NOW)
        second = SearchResultRanker.rank([a, b], "", freshness="latest", now=NOW)
        self.assertEqual([r.title for r in first], [r.title for r in second])
        self.assertEqual([r.title for r in first], ["A", "B"])  # provider order

    def test_age_days_populated_by_ranker(self):
        fresh = _result("Fresh", "https://a.example/1", "", _days_ago(0))
        undated = _result("Undated", "https://b.example/1", "")
        ranked = SearchResultRanker.rank([fresh, undated], "", freshness="latest", now=NOW)
        by_title = {r.title: r.age_days for r in ranked}
        self.assertEqual(by_title["Fresh"], 0)
        self.assertIsNone(by_title["Undated"])


# ── 3. recency_summary semantics ──


class TestRecencySummary(SimpleTestCase):
    def test_stale_when_newest_known_is_40_days_and_latest_requested(self):
        results = [
            _result("A", "https://a.example/1", "", _days_ago(40)),
            _result("B", "https://b.example/1", "", _days_ago(200)),
        ]
        summary = recency_summary(results, "latest", now=NOW)
        self.assertEqual(summary["known"], 2)
        self.assertEqual(summary["unknown"], 0)
        self.assertEqual(summary["newest_days"], 40)
        self.assertEqual(summary["oldest_days"], 200)
        self.assertIs(summary["stale"], True)

    def test_stale_is_none_when_no_dates_at_all(self):
        results = [_result("A", "https://a.example/1"), _result("B", "https://b.example/1")]
        summary = recency_summary(results, "latest", now=NOW)
        self.assertEqual(summary["known"], 0)
        self.assertEqual(summary["unknown"], 2)
        self.assertIsNone(summary["newest_days"])
        self.assertIsNone(summary["oldest_days"])
        self.assertIsNone(summary["stale"])

    def test_fresh_result_is_not_stale(self):
        results = [_result("A", "https://a.example/1", "", _days_ago(2))]
        self.assertIs(recency_summary(results, "latest", now=NOW)["stale"], False)
        self.assertIs(recency_summary(results, "recent", now=NOW)["stale"], False)

    def test_recent_window_is_30_days(self):
        results = [_result("A", "https://a.example/1", "", _days_ago(20))]
        self.assertIs(recency_summary(results, "recent", now=NOW)["stale"], False)
        results = [_result("A", "https://a.example/1", "", _days_ago(45))]
        self.assertIs(recency_summary(results, "recent", now=NOW)["stale"], True)
        # 45 days is stale for "recent" but not for... it is stale for "latest"
        # too, which is the point: both windows are enforced.
        self.assertIs(recency_summary(results, "latest", now=NOW)["stale"], True)


# ── 4. the [WEB] block contract ──


class TestWebBlockFormat(SimpleTestCase):
    def test_block_contains_freshness_dates_and_counts(self):
        results = [
            _result("Dated Story", "https://example.com/dated", "snippet one", "2026-10-20"),
            _result("Undated Story", "https://example.com/undated", "snippet two"),
        ]
        block = _format_block(results, freshness="latest", now=NOW)

        self.assertTrue(block.startswith("[WEB]\n"))
        self.assertTrue(block.endswith("\n[/WEB]"))
        self.assertIn("freshness=latest", block)
        self.assertIn("results=2", block)
        self.assertIn("dated=1", block)
        self.assertIn("unknown=1", block)
        self.assertIn("• Dated Story | date=2026-10-20 | source=example.com", block)
        self.assertIn("• Undated Story | date=unknown | source=example.com", block)
        self.assertIn("searched_at=", block)
        self.assertIn("+03:30", block)
        self.assertIn("note=date is the SOURCE PUBLICATION date, not the event date", block)
        # Not stale: one known date is inside the 7-day window.
        self.assertNotIn("recency_warning", block)

    def test_recency_warning_appears_only_when_stale(self):
        stale_results = [
            _result("Old Story", "https://example.com/old", "snippet", _days_ago(120)),
        ]
        stale_block = _format_block(stale_results, freshness="latest", now=NOW)
        self.assertIn("recency_warning=", stale_block)
        self.assertIn("recency_warning=120d", stale_block)

        fresh_results = [
            _result("New Story", "https://example.com/new", "snippet", _days_ago(1)),
        ]
        fresh_block = _format_block(fresh_results, freshness="latest", now=NOW)
        self.assertNotIn("recency_warning", fresh_block)

        # Never for a general query, however old the sources are.
        general_block = _format_block(stale_results, freshness="general", now=NOW)
        self.assertNotIn("recency_warning", general_block)
        self.assertIn("freshness=general", general_block)

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(_format_block([], freshness="latest", now=NOW), "")
        self.assertEqual(_format_block(None, freshness="latest", now=NOW), "")

    def test_at_most_five_results(self):
        results = [
            _result(f"Story {i}", f"https://example.com/{i}", "s", _days_ago(i))
            for i in range(9)
        ]
        block = _format_block(results, freshness="latest", now=NOW)
        self.assertIn("results=5", block)
        self.assertEqual(block.count("• "), 5)

    def test_stale_result_is_flagged_but_still_returned(self):
        """No silent dropping: an old source is shown AND marked as stale."""
        results = [
            _result("Dated But Old", "https://example.com/old", "body", _days_ago(9)),
            _result("Undated", "https://example.com/undated", "body"),
        ]
        block = _format_block(results, freshness="latest", now=NOW)
        self.assertIn("Dated But Old", block)
        self.assertIn(f"date={_days_ago(9)}", block)
        self.assertIn("recency_warning=9d", block)

    def test_block_stays_within_char_budget(self):
        results = [
            _result("T" * 400, f"https://example.com/{i}", "S" * 400, _days_ago(i))
            for i in range(5)
        ]
        block = _format_block(results, freshness="latest", now=NOW)
        self.assertLessEqual(len(block), 2400 + len("[WEB]\n[/WEB]"))
        self.assertIn("[truncated]", block)


# ── 6. currency preservation ──


class TestCurrencyPreservation(SimpleTestCase):
    """No digit, separator or unit may be rewritten on the way to the prompt."""

    SNIPPET = "قیمت دلار امروز ۱۰۴,۵۰۰ تومان"

    def test_snippet_survives_character_for_character(self):
        results = [
            _result("قیمت دلار", "https://www.tgju.org/news/1", self.SNIPPET, "2026-10-20"),
        ]
        block = _format_block(results, freshness="latest", now=NOW)
        self.assertIn(self.SNIPPET, block)
        # Explicitly assert the digits were not folded to ASCII and the unit
        # was not translated.
        self.assertIn("۱۰۴,۵۰۰", block)
        self.assertIn("تومان", block)
        self.assertNotIn("104,500", block)

    def test_snippet_is_single_line_and_clamped(self):
        messy = "خط اول\nخط دوم\t\tخط سوم " + ("x" * 400)
        results = [_result("T", "https://example.com/1", messy, "2026-10-20")]
        block = _format_block(results, freshness="latest", now=NOW)
        for line in block.splitlines():
            self.assertNotIn("\t", line)
        bullet_body = block.split("• T | date=2026-10-20 | source=example.com")[1]
        self.assertIn("خط اول خط دوم خط سوم", bullet_body)
        self.assertLess(len("خط اول خط دوم خط سوم " + "x" * 400), 500)
        self.assertNotIn("x" * 241, block)


# ── 5. honesty on retrieval failure ──


class TestRetrievalHonesty(unittest.IsolatedAsyncioTestCase):
    """A failed retrieval must be reported, never papered over."""

    async def test_web_search_without_results_reports_honestly(self):
        from botapp.agent.tools import _web_search
        from botapp.web import default_web_service

        with patch.object(default_web_service, "search", AsyncMock(return_value=[])):
            out = await _web_search("آخرین خبر دلار")

        self.assertEqual(out, "نتیجه‌ای در اینترنت یافت نشد.")
        # Nothing was retrieved, so nothing may be invented.
        self.assertNotIn("http", out)
        self.assertNotIn("date=", out)

    async def test_web_search_blank_query_is_not_a_fabricated_answer(self):
        from botapp.agent.tools import _web_search

        for blank in ("", "   "):
            out = await _web_search(blank)
            self.assertEqual(out, "لطفاً عبارت جستجو را مشخص کنید.")
            self.assertNotIn("http", out)

    async def test_fetch_url_failure_keeps_the_error_contract(self):
        from botapp.agent.tools import _fetch_url
        from botapp.web import default_web_service

        with patch.object(
            default_web_service, "fetch_page", AsyncMock(side_effect=RuntimeError("boom")),
        ):
            out = await _fetch_url("https://example.com/page")

        self.assertEqual(out, "خطا در باز کردن لینک: boom")
        self.assertNotIn("http", out)
        # No fabricated publication date on the failure path either.
        self.assertNotIn("تاریخ انتشار", out)

    async def test_fetch_url_labels_publication_date_as_source_date(self):
        from botapp.agent.tools import _fetch_url
        from botapp.web import default_web_service
        from botapp.web.models import WebDocument

        doc = WebDocument(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="عنوان",
            published_at="2026-10-19",
            text="متن صفحه",
        )
        with patch.object(default_web_service, "fetch_page", AsyncMock(return_value=doc)):
            out = await _fetch_url("https://example.com/page")

        self.assertIn("تاریخ انتشار: 2026-10-19", out)
        self.assertIn("نه تاریخ وقوع رویداد", out)
        self.assertIn("متن صفحه", out)

    async def test_fetch_url_marks_unknown_date_as_unknown(self):
        from botapp.agent.tools import _fetch_url
        from botapp.web import default_web_service
        from botapp.web.models import WebDocument

        doc = WebDocument(
            url="https://example.com/page",
            final_url="https://example.com/page",
            title="عنوان",
            published_at="",
            text="متن صفحه",
        )
        with patch.object(default_web_service, "fetch_page", AsyncMock(return_value=doc)):
            out = await _fetch_url("https://example.com/page")

        self.assertIn("تاریخ انتشار: نامشخص", out)

    async def test_web_search_passes_freshness_into_the_search_call(self):
        from botapp.agent.tools import _web_search
        from botapp.web import default_web_service

        results = [_result("Story", "https://example.com/1", "body", _days_ago(1))]
        fake = AsyncMock(return_value=results)
        with patch.object(default_web_service, "search", fake):
            out = await _web_search("آخرین خبر دلار")

        self.assertIn("date=", out)
        self.assertIn("| source=example.com", out)
        self.assertIn("https://example.com/1", out)
        _, kwargs = fake.call_args
        self.assertEqual(kwargs.get("freshness"), "latest")
        self.assertEqual(kwargs.get("limit"), 5)


# ── policy helper ──


class TestFreshnessIntent(SimpleTestCase):
    def test_latest(self):
        for text in (
            "آخرین خبر دلار",
            "جدیدترین نسخه پایتون",
            "قیمت دلار امروز",
            "همین الان چی شد",
            "breaking news about X",
            "What is the latest on the release?",
            "خبر فوری",
            "تازه‌ترین اخبار",  # ZWNJ variant
            "تازهترین اخبار",    # ZWNJ-free variant
        ):
            with self.subTest(text=text):
                self.assertEqual(freshness_intent(text), "latest")

    def test_recent(self):
        for text in (
            "این هفته چه خبر بود",
            "هفته گذشته چه اتفاقی افتاد",
            "این ماه چی شد",
            "اخیرا چه خبر",
            "recent developments in Django",
            "this week in tech",
        ):
            with self.subTest(text=text):
                self.assertEqual(freshness_intent(text), "recent")

    def test_general(self):
        for text in ("", "فرمول ریاضی انتگرال", "دلار چنده", "how do decorators work"):
            with self.subTest(text=text):
                self.assertEqual(freshness_intent(text), "general")


# ── model plumbing ──


class TestSearchResultModel(SimpleTestCase):
    def test_new_fields_are_keyword_defaults_and_serialized(self):
        res = SearchResult(title="T", url="https://example.com/1", snippet="S")
        self.assertEqual(res.published_at, "")
        self.assertIsNone(res.age_days)
        payload = res.to_dict()
        self.assertEqual(payload["published_at"], "")
        self.assertIsNone(payload["age_days"])

    def test_existing_positional_construction_still_works(self):
        res = SearchResult("T", "https://example.com/1", "S", "duckduckgo", 1, 9.5)
        self.assertEqual((res.source, res.rank, res.score), ("duckduckgo", 1, 9.5))
        self.assertEqual(res.published_at, "")


if __name__ == "__main__":
    unittest.main()
