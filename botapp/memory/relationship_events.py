"""Conservative social-event detection for Noya relationship state.

The classifier is intentionally narrow: it must not turn ordinary chat,
disagreement, friendly teasing or playful familiarity into relationship damage.
Every mismatch here becomes either a fabricated conflict or a missed apology, so
ambiguous text returns ``None``.
"""

from __future__ import annotations

import re

from botapp.emotions import EmotionalEvent

# Emoji/verbal cues that mark a warm or playful frame.  Their presence vetoes
# every negative classification for the same message.
_PLAYFUL_CUES = re.compile(
    r"(?:شوخ(?:ی|یه|ید|ی کردم)|هه+\s*ه|هاری\s*هار|😄|😁|😂|🤣|😆|😉|"
    r"ناز\s*نکن|لج\s*نکن|قهر\s*نکن|بامزه|خخخ|هیحی|جونگول)"
)
_AFFECTION_CUES = re.compile(
    r"(?:دوستت\s*دارم|عاشقتم|قربونت|فدات|بوست\s*دارم|❤️|🥰|😍)"
)

_APOLOGY = re.compile(
    r"(?:ببخش(?:ید|م|ی)|معذرت\s*می\s*خوام|معذرت|شرمنده|عذر\s*می\s*خوام|"
    r"اشتباه\s*کردم|غلط\s*کردم|اشتباه\s*از\s*من\s*بود|تقصیر\s*من\s*بود|"
    r"حق\s*با\s*تو\s*بود|کارم\s*اشتباه\s*بود)"
)
_ACK_OF_HARM = re.compile(
    r"(?:می\s*دونم\s*ناراحتت\s*کردم|می\s*فهمم\s*ناراحتت\s*کردم|"
    r"حق\s*داشتی|حرفت\s*درست\s*بود|حرف\s*حساب\s*داشتی|درست\s*می\s*گی\s*که\s*ناراحت\s*شدی)"
)
_BEHAVIORAL = re.compile(
    r"(?:عمل\s*[ًا]?\s*درستش\s*کردم|درستش\s*کردم|جبران\s*کردم|جبران\s*می\s*کنم|"
    r"نشون\s*دادم|دیگه\s*تکرار\s*نمی\s*کنم|از\s*این\s*به\s*بعد\s*حواسم\s*هست)"
)
_GRATITUDE = re.compile(
    r"(?:مرسی|ممنون|دمت\s*گرم|خیلی\s*خوبی|عالی\s*بود|دستت\s*درد\s*نکنه|لطف\s*کردی)"
)
_POSITIVE_NEWS = re.compile(
    r"(?:آفرین|چه\s*خبر\s*خوبی|مبارک|ذوق\s*کردم|خوشحال\s*شدم|قبول\s*شدم|برنده\s*شدم)"
)
# Criticism of the ANSWER quality — a real complaint that deserves an
# acknowledgement, but not a relationship injury.
_QUALITY_CRITICISM = re.compile(
    r"(?:(?:جواب|پاسخ)(?:ات|هات|ا|ای|ها|هایت)?[^.؟!]{0,40}"
    r"(?:مسخره|افتضاح|بیخود|بی\s*خود|ضعیف|بی\s*درد\s*بخور|به\s*درد\s*نمی\s*خور(?:ه|د)|"
    r"اشتباه|غلط|خوب\s*نیست|دقیق\s*نیست|سر\s*کاری|چرند)|"
    r"(?:خیلی\s*بد\s*جواب\s*می\s*دی|بد\s*جواب\s*می\s*دی|خوب\s*جواب\s*نمی\s*دی)|"
    r"جدی\s*می\s*گم[^.؟!]{0,30}(?:مسخره|افتضاح|بد\s*جواب|دقت\s*نمی))"
)
_SERIOUS_INSULT = re.compile(
    r"(?:خفه\s*شو|گم\s*شو|گمشو|احمق|بی\s*شعور|بی\s*ادب|کثافت|مغز\s*نداری|"
    r"ازت\s*متنفرم|حالم\s*ازت\s*به\s*هم\s*می\s*خوره|نفهم|پوچ\s*گفتی)"
)
_MILD_DISRESPECT = re.compile(
    r"(?:بی\s*احترامی|بی\s*ادبی|بد\s*حرف\s*زدی|حرفت\s*بد\s*بود|توهین|"
    r"بی\s*احساس|چقدر\s*رو\s*داری)"
)
_DISAGREEMENT = re.compile(
    r"(?:موافق\s*نیستم|به\s*نظرم\s*اشتباهه|غلطه\b|درست\s*نیست\b|اشتباه\s*می\s*گی|قبول\s*ندارم)"
)
_REPEAT_CUE = re.compile(r"(?:باز\s*هم|دوباره|همیشه|هر\s*بار|مثل\s*همیشه)")


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\u200c", " ").casefold().split())


def classify_social_event(text: str | None) -> EmotionalEvent | None:
    """Return a high-confidence event; ordinary or ambiguous chat returns None."""
    t = _clean(text or "")
    if not t:
        return None

    playful = bool(_PLAYFUL_CUES.search(t)) or bool(_AFFECTION_CUES.search(t))

    # Repair events win over everything: they are explicit and unambiguous.
    if _APOLOGY.search(t):
        return EmotionalEvent("sincere_apology", confidence=0.9)
    if _ACK_OF_HARM.search(t):
        return EmotionalEvent("acknowledgement_of_harm", confidence=0.9)
    if _BEHAVIORAL.search(t):
        return EmotionalEvent("behavioral_improvement", confidence=0.75)

    if playful:
        # Friendly teasing, playful insults between familiar users and warm
        # messages are never relationship damage.
        return EmotionalEvent("playful_teasing", confidence=0.8)

    if _QUALITY_CRITICISM.search(t):
        # Criticism of the model's answers: acknowledge it, do not take damage.
        return EmotionalEvent("quality_criticism", confidence=0.8)

    if _GRATITUDE.search(t):
        return EmotionalEvent("gratitude", confidence=0.8)
    if _POSITIVE_NEWS.search(t):
        return EmotionalEvent("positive_news", confidence=0.75)

    serious = _SERIOUS_INSULT.search(t)
    mild = _MILD_DISRESPECT.search(t)
    if serious or mild:
        repeated = bool(_REPEAT_CUE.search(t))
        return EmotionalEvent(
            "repeated_disrespect" if repeated else ("serious_disrespect" if serious else "mild_disrespect"),
            severity=45 if repeated else (30 if serious else 12),
            confidence=0.85,
        )

    if _DISAGREEMENT.search(t):
        return EmotionalEvent("normal_disagreement", confidence=0.7)
    return None
