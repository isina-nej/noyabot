"""Persian response formatting helpers for the admin agent.

Besides the Persian formatting helpers this module owns the *only* structural
filter for provider "internal planning annotations".  Noya's model backends
(9Router / OpenAI-compatible proxies) sometimes return their own scratch notes
inside the assistant answer, e.g.::

    → skipped: پیاده‌سازی کامل نمونه کدهای موازی، add when نیاز به بنچمارک بود.

Those notes must never reach a Telegram chat.  A single line-anchored regex is
not enough (markers vary, the arrow is often missing, notes get appended to an
otherwise valid sentence), so detection is a small structural classifier:

* :func:`strip_internal_annotations` — fence-aware line classifier.
* :func:`clean_model_response` — main entry used by the agent loop.
* :func:`sanitize_outgoing_text` — the single outbound gate for delivery paths.

All of them are deterministic and idempotent, and none of them ever touches
text inside a fenced code block, an inline code span, a URL or a markdown
table row.
"""

from __future__ import annotations

import re

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# ---------------------------------------------------------------------------
# Internal-annotation vocabulary (explicit sets — no single mega-regex)
# ---------------------------------------------------------------------------

#: Optional bullet/arrow marker a planning note may be prefixed with.
NOTE_LINE_MARKERS: tuple[str, ...] = (
    "→",
    "->",
    "⇒",
    "▸",
    "•",
    "—",
    "–",
    "-",
    "*",
    "#",
)

#: Keyword a note line starts with (case-insensitive), followed by a separator.
NOTE_LINE_KEYWORDS: tuple[str, ...] = (
    "add when",
    "skipped",
    "skip",
    "notes",
    "note",
    "internal",
    "planning",
    "plan",
    "todo",
    "رد شده",
    "رد شد",
    "یادداشت",
    "برنامه",
    "صرف‌نظر",
    "صرف نظر",
)

#: Separator characters allowed right after a note keyword.
NOTE_KEYWORD_SEPARATORS: tuple[str, ...] = (":", "：", "=", "＝")

#: Token that, on its own line, is never user-facing prose.
ADD_WHEN_TOKEN = "add when"

#: Tokens that together with :data:`ADD_WHEN_TOKEN` mark a line as internal.
NOTE_PAIR_TOKENS: tuple[str, ...] = ("skipped", "skip", "رد شد", "یادداشت")

#: Only the beginning of a line is inspected for the two-token pair.
NOTE_SCAN_LIMIT = 160

#: Separator that may carry an appended ``add when …`` tail on a valid line.
#: The optional ``:``/``=`` tolerates ``… - add when: condition`` spellings.
_TRAILING_ANNOTATION_RE = re.compile(
    r"(?:،|,|;|—| - )\s*add\s+when\s*[:=：＝]?\s*",
    re.IGNORECASE,
)

_ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
_NBSP = "\u00a0"
#: Leading characters ignored before a marker/keyword is looked for.
_LINE_LEAD_TRIM = " \t" + _NBSP + _ZERO_WIDTH_CHARS
#: Emphasis/quote characters tolerated between a keyword and its separator.
_EMPHASIS_TRIM = "*_`«»\"'“”‘’" + _LINE_LEAD_TRIM

_FENCE_DELIMITERS = ("```", "~~~")

_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _iter_lines(text: str):
    """Yield ``(line, protected)`` pairs.

    ``protected`` is True for anything inside a fenced code block (including
    the fence delimiter lines themselves), which is never modified.
    """
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith(_FENCE_DELIMITERS):
                in_fence = True
                fence_marker = stripped[:3]
                yield line, True
                continue
            yield line, False
            continue
        yield line, True
        if stripped.startswith(fence_marker):
            in_fence = False
            fence_marker = ""


def _protected_spans(line: str) -> list[tuple[int, int]]:
    """Character spans of inline code spans and URLs inside *line*."""
    spans: list[tuple[int, int]] = [
        (match.start(), match.end()) for match in _INLINE_CODE_RE.finditer(line)
    ]
    spans.extend(
        (match.start(), match.end()) for match in _URL_RE.finditer(line)
    )
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _mask_protected_spans(line: str) -> str:
    """Replace inline-code/URL characters with NUL so patterns cannot match."""
    spans = _protected_spans(line)
    if not spans:
        return line
    chars = list(line)
    for start, end in spans:
        for index in range(start, min(end, len(chars))):
            chars[index] = "\x00"
    return "".join(chars)


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") or stripped.endswith("|")


def _strip_leading_markers(line: str) -> str:
    """Drop leading whitespace plus at most one bullet/arrow marker."""
    body = line.lstrip(_LINE_LEAD_TRIM)
    for marker in NOTE_LINE_MARKERS:
        if body.startswith(marker):
            return body[len(marker):].lstrip(_EMPHASIS_TRIM)
    return body


def _line_is_internal_heading(masked: str) -> bool:
    """True when the line *starts* with a note keyword + separator."""
    body = _strip_leading_markers(masked)
    folded = body.casefold()
    for keyword in NOTE_LINE_KEYWORDS:
        if not folded.startswith(keyword):
            continue
        rest = body[len(keyword):].lstrip(_EMPHASIS_TRIM)
        if rest[:1] and rest[0] in NOTE_KEYWORD_SEPARATORS:
            return True
    return False


def _line_is_internal_pair(masked: str) -> bool:
    """True when ``add when`` and a skip token co-occur near the line start."""
    window = masked[:NOTE_SCAN_LIMIT].casefold()
    if ADD_WHEN_TOKEN not in window:
        return False
    return any(token in window for token in NOTE_PAIR_TOKENS)


def _trailing_annotation_cut(masked: str) -> int | None:
    """Index where an appended ``… , add when …`` tail starts, if any."""
    match = _TRAILING_ANNOTATION_RE.search(masked)
    return match.start() if match else None


def _classify_line(line: str) -> str | None:
    """Return the user-facing form of *line*, or ``None`` when it is internal."""
    if not line.strip(_LINE_LEAD_TRIM):
        return line
    if _is_table_row(line):
        return line
    masked = _mask_protected_spans(line)
    if _line_is_internal_heading(masked):
        return None
    if _line_is_internal_pair(masked):
        return None
    cut = _trailing_annotation_cut(masked)
    if cut is not None:
        kept = line[:cut].rstrip(_LINE_LEAD_TRIM)
        return kept or None
    return line


def _without_zero_width(line: str) -> str:
    """*line* with zero-width characters removed (used only for re-checking)."""
    return "".join(char for char in line if char not in _ZERO_WIDTH_CHARS)


# ---------------------------------------------------------------------------
# Public filters
# ---------------------------------------------------------------------------


def strip_internal_annotations(text: str) -> str:
    """Remove provider planning annotations from *text*.

    Structural, deterministic and idempotent.  A line is dropped when, outside
    fenced code and after an optional marker from :data:`NOTE_LINE_MARKERS`, it
    starts with a keyword from :data:`NOTE_LINE_KEYWORDS` followed by ``:`` /
    ``=``; when it pairs ``add when`` with a skip token near the start; or when
    it carries an appended ``… , add when …`` tail (then only the tail goes).
    Fenced code, inline code, URLs and markdown table rows are left untouched.
    """
    if not text:
        return ""
    kept: list[str] = []
    for line, protected in _iter_lines(text):
        if protected:
            kept.append(line)
            continue
        cleaned = _classify_line(line)
        if cleaned is None:
            continue
        kept.append(cleaned)
    result = "\n".join(kept)
    return result if result.strip() else ""


def clean_model_response(text: str) -> str:
    """Main entry: strip internal planning notes, preserving fenced code."""
    return strip_internal_annotations(text or "").strip()


def sanitize_outgoing_text(text: str) -> str:
    """Single outbound gate for AI text about to be delivered to a user.

    Applies :func:`clean_model_response`, then a defensive pass that re-checks
    every surviving line (including lines whose zero-width characters could
    hide a marker) and drops anything still classified as a note.  Fenced code
    blocks are never altered.  Idempotent; returns ``""`` when nothing human
    remains.
    """
    original = text or ""
    cleaned = clean_model_response(original)
    if not cleaned:
        return ""
    kept: list[str] = []
    for line, protected in _iter_lines(cleaned):
        if protected:
            kept.append(line)
            continue
        if _classify_line(line) is None:
            continue
        restored = _without_zero_width(line)
        if restored != line and _classify_line(restored) is None:
            # Zero-width characters were hiding a note — drop the whole line
            # rather than silently mangling legitimate text elsewhere.
            continue
        kept.append(line)
    result = "\n".join(kept)
    value = result.strip() if result.strip() else ""
    if value != original.strip():
        try:
            from botapp.observability.instrumentation import internal_artifact_detected, output_sanitized
            if original.strip() and not value:
                output_sanitized(empty=True)
            else:
                output_sanitized(dropped_lines=max(0, len(original.splitlines()) - len(value.splitlines())))
            if len(value) < len(original):
                internal_artifact_detected(component="response_sanitizer")
        except Exception:
            pass
    return value


# ---------------------------------------------------------------------------
# Persian formatting helpers
# ---------------------------------------------------------------------------


def fa_number(value: int | float) -> str:
    """Render an integer with Persian digits and the Persian thousands mark."""
    try:
        text = f"{int(value):,}"
    except (TypeError, ValueError):
        text = str(value)
    return text.translate(PERSIAN_DIGITS).replace(",", "٬")


def fa_duration(minutes: int | None) -> str:
    if minutes is None:
        return "دائمی"
    if minutes % 60 == 0 and minutes >= 60:
        return f"{fa_number(minutes // 60)} ساعت"
    return f"{fa_number(minutes)} دقیقه"


RISK_LABELS = {
    "low": "کم",
    "medium": "متوسط",
    "high": "زیاد",
}


def confirmation_preview(
    *,
    verb: str,
    target_label: str = "",
    target_user_id: int | None = None,
    duration_minutes: int | None = None,
    delay_minutes: int = 0,
    reason: str = "",
    value=None,
    requester_name: str = "",
    group_name: str = "",
    risk_level: str = "medium",
) -> str:
    lines = ["⚠️ تأیید عملیات مدیریتی", "", f"عملیات: {verb}"]
    if value is not None and str(value) != "":
        # Show the exact free-text/numeric value so the admin approves precisely
        # what will be applied (defends against injected/ambiguous values).
        lines.append(f"مقدار: {value}")
    if target_label:
        lines.append(f"کاربر: {target_label}")
    if target_user_id is not None:
        lines.append(f"شناسه: {fa_number(target_user_id)}")
    if delay_minutes:
        lines.append(f"تأخیر شروع: {fa_duration(delay_minutes)}")
    if duration_minutes is not None or verb.strip().endswith(("قفل", "سکوت", "محدودکردن کاربر")):
        lines.append(f"مدت: {fa_duration(duration_minutes)}")
    if reason:
        lines.append(f"دلیل: {reason}")
    if requester_name:
        lines.append(f"درخواست‌دهنده: {requester_name}")
    if group_name:
        lines.append(f"گروه: {group_name}")
    lines.append(f"سطح ریسک: {RISK_LABELS.get(risk_level, risk_level)}")
    lines.append("")
    lines.append("این عملیات انجام شود؟")
    return "\n".join(lines)
