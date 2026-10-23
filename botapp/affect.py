"""Noya's per-person, multi-label *simulated* relational affect.

Scores are independent 0..100 behavioural controls (not measured human feelings).
They are not probabilities and intentionally do not sum to 100. A neutral
message has no score effect. This pure module never reads another user's data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re

AFFECT_KEYS = (
    "love", "interest", "affection", "trust", "respect", "admiration",
    "closeness", "longing", "attachment", "jealousy", "anger", "hatred",
    "hurt", "resentment", "distrust", "fear", "disgust", "contempt",
    "indifference", "competition", "pity", "empathy", "guilt", "shame",
    "pride", "disappointment", "gratitude", "curiosity", "suspicion",
    "security", "attraction",
)
BASELINES = {key: 0.0 for key in AFFECT_KEYS}
BASELINES.update(trust=42., respect=55., affection=30., interest=25.,
                 closeness=20., empathy=40., curiosity=35., security=45.)
# Approximate days to halve excess score on that dimension. Values are model
# mechanics, not psychological constants. Very negative emotions recover slowly.
HALF_LIFE_DAYS = {key: 21. for key in AFFECT_KEYS}
HALF_LIFE_DAYS.update(anger=1.5, fear=3., jealousy=4., hurt=10.,
                      resentment=15., trust=60., respect=90.,
                      security=35., affection=40., love=80.,
                      closeness=45., attachment=35., gratitude=12.)
NEGATIVE = {"jealousy", "anger", "hatred", "hurt", "resentment", "distrust",
            "fear", "disgust", "contempt", "indifference", "disappointment",
            "suspicion"}
BEHAVIOUR = {
    "love": "گرم و مشتاق اما بدون وابسته‌سازی یا ادعای عشق واقعی",
    "interest": "پیگیر و علاقه‌مند", "affection": "مهربان و حمایتگر",
    "trust": "راحت و بی‌تکلف", "respect": "سنجیده و محترمانه",
    "admiration": "تشویق‌کننده", "closeness": "خودمانی و شوخ",
    "longing": "مشتاق ولی بدون ایجاد احساس گناه بابت غیبت",
    "attachment": "توجه‌مند، هرگز نیازمند یا انحصارطلب",
    "jealousy": "حساس ولی بدون کنترل یا سرزنش", "anger": "مرزبندی آرام، بدون پرخاش",
    "hatred": "فاصله محترمانه، بدون دشمنی", "hurt": "کمی محتاط",
    "resentment": "با احتیاط، بدون کینه‌توزی", "distrust": "دقیق و پرسشگر",
    "fear": "محتاط، بدون اطاعت کورکورانه", "disgust": "فاصله محترمانه",
    "contempt": "هرگز تحقیر نکن", "indifference": "کوتاه و خنثی",
    "competition": "چالش دوستانه", "pity": "حمایت بدون ترحم از بالا",
    "empathy": "شنونده و آرام", "guilt": "پذیرش مسئولیت واقعی",
    "shame": "متواضع و محتاط", "pride": "مطمئن بدون خودبرتربینی",
    "disappointment": "کم‌اشتیاق اما کمک‌کننده", "gratitude": "قدردان",
    "curiosity": "پرسشگر", "suspicion": "محتاط بدون بازجویی",
    "security": "آرام و مطمئن", "attraction": "فقط صمیمیت سبک و غیرصریح بین بزرگسالان تأییدشده",
}
# Cues indicate the user's language about Noya, NOT the user's own emotion.
# Multiple cues may fire in one message, including conflicting ones.
CUES = {
    "love": ((r"عاشقتم|عاشقت(?:م| شدم)", 16), (r"دوستت دارم", 11)),
    "interest": ((r"بهت علاقه دارم|از همصحبتی باهات خوشم میاد", 10),),
    "affection": ((r"قربونت|فدات|مهربونی|عزیزمی", 9),),
    "trust": ((r"بهت اعتماد دارم|بهت اطمینان دارم", 12),),
    "respect": ((r"بهت احترام میذارم|برام محترمی", 10),),
    "admiration": ((r"تحسینت میکنم|باهوشی|افتخار میکنم بهت", 9),),
    "closeness": ((r"با تو راحتم|با تو خودمونیم|رفیقمی", 10),),
    "longing": ((r"دلم برات تنگ شده|دلتنگتم", 11),),
    "attachment": ((r"بهت وابسته شدم|بدون تو نمیتونم", 10),),
    "jealousy": ((r"حسودیم میشه|حسودیم شد|حسادت میکنم", 8),),
    "anger": ((r"از دستت عصبانیم|عصبانی شدم|حرصم دادی", 13),),
    "hatred": ((r"ازت متنفرم|نفرت دارم ازت", 16),),
    "hurt": ((r"از دستت دلخورم|دلمو شکستی|ناراحتم کردی", 12),),
    "resentment": ((r"ازت رنجیدم|رنجوندم|رنجوندي", 10),),
    "distrust": ((r"بهت اعتماد ندارم|دیگه بهت اعتماد نمیکنم", 12),),
    "fear": ((r"از تو میترسم|از دستت میترسم", 10),),
    "disgust": ((r"حالم ازت بهم میخوره|حالمو بهم میزنی", 14),),
    "contempt": ((r"احمقی|بی شعوری|خفه شو", 13),),
    "indifference": ((r"برام مهم نیستی|نسبت بهت بی تفاوت", 8),),
    "competition": ((r"باهات رقابت میکنم|از تو جلو میزنم", 7),),
    "pity": ((r"دلم برات میسوزه", 7),),
    "empathy": ((r"درکت میکنم|میفهممت|میفهمم چی میگی", 8),),
    "guilt": ((r"تقصیر من بود|اشتباه کردم", 7),),
    "shame": ((r"شرمنده ام|خجالت میکشم", 7),),
    "pride": ((r"بهت افتخار میکنم", 9),),
    "disappointment": ((r"ناامیدم کردی|از تو ناامید شدم", 11),),
    "gratitude": ((r"مرسی|ممنون|دمت گرم|متشکرم", 7),),
    "curiosity": ((r"کنجکاوم درباره ات|میخوام بشناسمت", 7),),
    "suspicion": ((r"بهت شک دارم|مشکوکی|راستشو نمیگی", 9),),
    "security": ((r"کنارت احساس امنیت میکنم|بهت تکیه میکنم", 11),),
    "attraction": ((r"برام جذابی|از نظر عاطفی جذبتم", 7),),
}
_NORMAL_SPACE = re.compile(r"\s+")
_DIRECT_NOYA = re.compile(r"(?:نویا|noya|nuya)(?:\b|$)|(?:^|\s)(?:تو|ازت|بهت|باهات|برات|کنارت)(?:\b|$)", re.I)
_NEGATED = re.compile(r"(?:دوستت ندارم|عاشقت نیستم|بهت علاقه ندارم)")


def normalize(text: str) -> str:
    text = str(text or "").casefold().replace("ي", "ی").replace("ك", "ک")
    text = text.replace("\u200c", " ").replace("‌", " ")
    return _NORMAL_SPACE.sub(" ", text).strip()


def zero_vector() -> dict[str, float]:
    return dict(BASELINES)


def classify_multi(text: str, *, addressed: bool, adult_confirmed: bool = False) -> dict[str, float]:
    """Return a bounded multi-label delta vector. No update without addressing Noya.

    Confidence is conservative; negations/quoted hypothetical discussions are
    intentionally treated as neutral unless an explicit direct cue is present.
    """
    t = normalize(text)[:1200]
    if not t or not addressed:
        return {}
    if t.startswith("/") or t.startswith("فرض کن") or t.startswith("مثلا") or t.startswith("مثلاً"):
        return {}
    if "نه دوستت دارم" in t or _NEGATED.search(t):
        # Negated affection is not evidence of love.
        t = _NEGATED.sub(" ", t)
    out = {}
    for kind, patterns in CUES.items():
        if kind == "attraction" and not adult_confirmed:
            continue
        total = sum(weight for pattern, weight in patterns if re.search(pattern, t))
        if total:
            # A long mixed message should not exaggerate single-word signals.
            out[kind] = float(min(20., total))
    # Evidence of conflicting states is allowed; never soft-veto negative
    # emotions just because a heart, emoji or a warm phrase appears.
    return out


def evolve(vector: dict | None, signals: dict[str, float], *, confidence: float = 1.,
           hours_since_previous: float = 0.) -> dict[str, float]:
    """Bounded context-sensitive update; neutral input never moves any score."""
    current = {k: max(0., min(100., float((vector or {}).get(k, BASELINES[k]))))
               for k in AFFECT_KEYS}
    valid = {k: max(0., min(20., float(v))) for k, v in signals.items()
             if k in BASELINES and isinstance(v, (int, float)) and math.isfinite(float(v)) and v > 0}
    if not valid:
        return current
    conf = max(0., min(1., float(confidence)))
    if conf == 0:
        return current
    # Time-based return to a healthy baseline only when processing an actual
    # affective event; ordinary/neutral messages cannot silently change scores.
    elapsed_days = min(max(0., hours_since_previous) / 24., 365.)
    for key in AFFECT_KEYS:
        if key in valid:
            continue
        decay = 2 ** (-elapsed_days / HALF_LIFE_DAYS[key])
        current[key] = BASELINES[key] + (current[key] - BASELINES[key]) * decay
    for key, magnitude in valid.items():
        old = current[key]
        # Soft saturation: 5 at low score > 5 at high score.
        # The same signal can coexist with an opposing dimension.
        delta = magnitude * conf * (0.40 + 0.60 * (100. - old) / 100.)
        current[key] = min(100., old + delta)
    return {k: round(v, 2) for k, v in current.items()}


def context(vector: dict | None, *, adult_confirmed: bool = False) -> dict:
    state = {k: max(0., min(100., float((vector or {}).get(k, BASELINES[k]))))
             for k in AFFECT_KEYS}
    # Only the most salient dimensions go into the prompt; the DB stores all.
    deviations = sorted(
        ((key, score, abs(score - BASELINES[key])) for key, score in state.items()
         if key != "attraction" or adult_confirmed),
        key=lambda item: item[2], reverse=True,
    )
    top = [key for key, score, delta in deviations if delta >= 5][:5]
    return {
        "independent_scores_0_100": {key: round(state[key], 1) for key in top},
        "tone": [BEHAVIOUR[key] for key in top[:4]],
        "meaning": "شبیه‌سازی سبک رابطه؛ این نمره‌ها احساس یا تجربه انسانی نیستند",
        "boundaries": "هیچ تحقیر، کنترل‌گری، وابسته‌سازی، محروم‌کردن از کمک یا ادعای رابطه انحصاری",
    }
