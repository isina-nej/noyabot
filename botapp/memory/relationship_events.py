"""Conservative social-event detection for Noya relationship state."""

from __future__ import annotations

import re

from botapp.emotions import EmotionalEvent


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\u200c", " ").casefold().split())


def classify_social_event(text: str | None) -> EmotionalEvent | None:
    """Return a high-confidence event; ordinary or ambiguous chat returns None."""
    t = _clean(text or "")
    if not t:
        return None

    if re.search(r"(?:ببخشید|معذرت می ?خوام|اشتباه کردم|غلط کردم|حق با تو بود)", t):
        return EmotionalEvent("sincere_apology", confidence=0.9)
    if re.search(r"(?:می ?دونم ناراحتت کردم|می ?فهمم ناراحتت کردم|حق داشتی|حرفت درست بود)", t):
        return EmotionalEvent("acknowledgement_of_harm", confidence=0.9)
    if re.search(r"(?:عملًا درستش کردم|درستش کردم|جبران کردم|نشون دادم)", t):
        return EmotionalEvent("behavioral_improvement", confidence=0.75)

    # Friendly teasing and sarcasm are explicitly kept out of negative scoring.
    if re.search(r"(?:شوخی کردم|هار هار|خودت لجبازی|تو خیلی لجبازی|ناز نکن)", t):
        return EmotionalEvent("playful_teasing", confidence=0.8)
    if re.search(r"(?:مرسی|ممنون|دمت گرم|خیلی خوبی|عالی بود|دوستت دارم)", t):
        return EmotionalEvent("gratitude", confidence=0.8)
    if re.search(r"(?:آفرین|چه خبر خوبی|مبارک|ذوق کردم|خوشحال شدم)", t):
        return EmotionalEvent("positive_news", confidence=0.75)

    serious = re.search(r"(?:خفه شو|گمشو|احمق|بی شعور|بی‌شعور|لعنتی|ازت متنفرم)", t)
    mild = re.search(r"(?:بی ادبی|بی‌ادبی|بد حرف زدی|حرفت بد بود|بی احترامی|بی‌احترامی)", t)
    if serious or mild:
        repeated = "باز هم" in t or "دوباره" in t
        return EmotionalEvent(
            "repeated_disrespect" if repeated else ("serious_disrespect" if serious else "mild_disrespect"),
            severity=45 if repeated else (30 if serious else 12),
            confidence=0.85,
        )
    return None
