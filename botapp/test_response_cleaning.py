"""Regression tests for the provider-annotation filter (botapp/agent/responses.py).

The P0 bug: Noya delivered internal planning notes such as
``→ skipped: پیاده‌سازی کامل نمونه کدهای موازی، add when …`` to Telegram users.
"""

from django.test import SimpleTestCase

from botapp.agent.responses import (
    clean_model_response,
    sanitize_outgoing_text,
    strip_internal_annotations,
)


class ModelResponseCleaningTests(SimpleTestCase):
    # ── the three original expectations (must keep passing) ──
    def test_internal_planning_note_is_not_sent(self):
        raw = "خوبم 🙂\n→ skipped: enthusiasm, add when: funny.\nبگو ببینم"
        self.assertEqual(clean_model_response(raw), "خوبم 🙂\nبگو ببینم")

    def test_internal_note_without_colon_and_with_persian_text_is_not_sent(self):
        raw = "پاسخ فنی\n→ skipped: پیاده‌سازی کامل نمونه کدهای موازی، add when نیاز به بنچمارک یا تست عملیاتی بود."
        self.assertEqual(clean_model_response(raw), "پاسخ فنی")

    def test_code_blocks_and_normal_arrows_are_preserved(self):
        raw = "این فلش را نگه دار: →\n```text\n→ skipped: x, add when: y\n```"
        self.assertEqual(clean_model_response(raw), raw)

    # ── marker variants ──
    def test_every_marker_variant_is_removed(self):
        for marker in ("→", "->", "⇒", "▸", "•", "-", "*", "—", "–", "#"):
            with self.subTest(marker=marker):
                raw = f"خط اول\n{marker} skipped: internal reasoning, add when: needed\nخط سوم"
                self.assertEqual(
                    clean_model_response(raw),
                    "خط اول\nخط سوم",
                    msg=f"marker {marker!r} leaked",
                )

    def test_marker_variant_without_add_when_is_removed(self):
        raw = "پاسخ\n⇒ skipped: no benchmark data\nپایان"
        self.assertEqual(clean_model_response(raw), "پاسخ\nپایان")

    # ── notes with no arrow at all ──
    def test_note_without_any_arrow_is_removed(self):
        raw = "جواب\nskipped: پیاده‌سازی موازی\nخداحافظ"
        self.assertEqual(clean_model_response(raw), "جواب\nخداحافظ")

    def test_note_keyword_needs_a_separator(self):
        # A sentence that merely starts with the same letters is real prose.
        raw = "planning the trip: فردا حرکت می‌کنیم"
        self.assertEqual(clean_model_response(raw), raw)

    def test_skipped_without_add_when_is_removed(self):
        raw = "پاسخ کوتاه\nskipped: lack of data\n—"
        self.assertEqual(clean_model_response(raw), "پاسخ کوتاه\n—")

    def test_add_when_without_skipped_is_removed(self):
        raw = "پاسخ\nadd when: کاربر دوباره بپرسد\nپایان"
        self.assertEqual(clean_model_response(raw), "پاسخ\nپایان")

    # ── Persian note keywords ──
    def test_persian_notes_are_removed(self):
        raw = "سلام\nیادداشت: این بخش را حذف کن\nرد شد: نمونه کد موازی\nخوبی؟"
        self.assertEqual(clean_model_response(raw), "سلام\nخوبی؟")

    def test_persian_note_with_marker_is_removed(self):
        raw = "متن\n→ یادداشت: بعداً اضافه کن\nمتن دوم"
        self.assertEqual(clean_model_response(raw), "متن\nمتن دوم")

    # ── trailing annotation appended to an otherwise valid line ──
    def test_trailing_add_when_annotation_is_cut_from_valid_line(self):
        raw = "این پاسخ کامل است، add when کاربر بیشتر پرسید جزئیات بدهم"
        self.assertEqual(clean_model_response(raw), "این پاسخ کامل است")

    def test_trailing_dash_annotation_is_cut(self):
        raw = "پاسخ درست است - add when: لازم شد"
        self.assertEqual(clean_model_response(raw), "پاسخ درست است")

    def test_line_that_is_only_a_trailing_annotation_is_dropped(self):
        raw = "پاسخ\n، add when: never\nپایان"
        self.assertEqual(clean_model_response(raw), "پاسخ\nپایان")

    # ── nothing left ──
    def test_text_that_is_only_a_note_becomes_empty(self):
        self.assertEqual(clean_model_response("→ skipped: x, add when: y"), "")
        self.assertEqual(strip_internal_annotations("→ skipped: x, add when: y"), "")

    # ── idempotency ──
    def test_cleaning_is_idempotent(self):
        samples = [
            "خوبم 🙂\n→ skipped: enthusiasm, add when: funny.\nبگو ببینم",
            "این پاسخ کامل است، add when کاربر بیشتر پرسید جزئیات بدهم",
            "سلام\nیادداشت: حذف\n```python\n→ skipped: keep me\n```\nپایان",
            "→ skipped: only a note",
            "",
        ]
        for raw in samples:
            with self.subTest(raw=raw):
                once = clean_model_response(raw)
                self.assertEqual(clean_model_response(once), once)
                sanitized = sanitize_outgoing_text(raw)
                self.assertEqual(sanitize_outgoing_text(sanitized), sanitized)

    # ── fence preservation (``` and ~~~) ──
    def test_backtick_fence_is_preserved_byte_identical(self):
        raw = "متن\n```python\nprint('hi')  # → skipped: x, add when: y\nskipped: z\n```\nپایان"
        self.assertEqual(clean_model_response(raw), raw)

    def test_tilde_fence_is_preserved_byte_identical(self):
        raw = "متن\n~~~\n→ skipped: x, add when: y\nnote: preserved\n~~~\nپایان"
        self.assertEqual(clean_model_response(raw), raw)

    def test_note_outside_fence_is_removed_fence_is_kept(self):
        raw = (
            "توضیح\n"
            "→ skipped: پیاده‌سازی کامل، add when نیاز بود\n"
            "```python\n"
            "# → skipped: keep this comment, add when: never\n"
            "```"
        )
        expected = (
            "توضیح\n"
            "```python\n"
            "# → skipped: keep this comment, add when: never\n"
            "```"
        )
        self.assertEqual(clean_model_response(raw), expected)

    # ── inline code / URLs / tables ──
    def test_inline_code_span_is_preserved(self):
        raw = "دستور `skipped: x, add when: y` را نگه دار"
        self.assertEqual(clean_model_response(raw), raw)

    def test_url_is_preserved(self):
        raw = "ببین https://example.com/skipped:note?add_when=1 تمام"
        self.assertEqual(clean_model_response(raw), raw)

    def test_markdown_table_row_is_preserved(self):
        raw = "| ستون | skipped: x |\n|---|---|\n| ۱ | ۲ |"
        self.assertEqual(clean_model_response(raw), raw)

    # ── must NOT be over-eager ──
    def test_persian_sentence_with_vaghti_and_lone_arrow_is_preserved(self):
        raw = "وقتی کاربر می‌پرسد، کوتاه جواب بده → و منتظر بمان"
        self.assertEqual(clean_model_response(raw), raw)

    def test_plain_persian_answer_is_untouched(self):
        raw = "سلام! خوبی؟\nمن نویا هستم.\n\nچه کاری برات انجام بدم؟"
        self.assertEqual(clean_model_response(raw), raw)


class SanitizeOutgoingTextTests(SimpleTestCase):
    def test_gate_applies_the_same_classifier(self):
        raw = "سلام\n→ skipped: چیز داخلی، add when لازم شد\nخوبی؟"
        self.assertEqual(sanitize_outgoing_text(raw), "سلام\nخوبی؟")

    def test_gate_returns_empty_for_note_only_text(self):
        self.assertEqual(sanitize_outgoing_text("→ skipped: x, add when: y"), "")

    def test_gate_preserves_fenced_code(self):
        raw = "جمله\n```\nskipped: keep me\n```"
        self.assertEqual(sanitize_outgoing_text(raw), raw)

    def test_gate_is_idempotent(self):
        raw = "خوبم 🙂\n⇒ skipped: internal, add when: later\nبگو ببینم"
        once = sanitize_outgoing_text(raw)
        self.assertEqual(sanitize_outgoing_text(once), once)
        self.assertNotIn("skipped", once)

    def test_zero_width_hidden_note_is_dropped(self):
        # A zero-width space inside the keyword must not smuggle the note out.
        raw = "سلام\nskip\u200bped: hidden note\nدنیا"
        self.assertEqual(sanitize_outgoing_text(raw), "سلام\nدنیا")

    def test_gate_leaves_legitimate_zero_width_text_alone(self):
        raw = "می‌خواهم بروم\nنمونه‌کدها خوب‌اند"
        self.assertEqual(sanitize_outgoing_text(raw), raw)
