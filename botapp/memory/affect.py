"""Django adapter for simulated per-Telegram-identity affect and fictional lore.

Can be called by the outer message observer (even when Noya does not answer)
and by normal chat before composing the system/user prompt. Both paths are
idempotent through a single global user-scoped row.
"""
from __future__ import annotations

import re
from django.db import transaction
from django.utils import timezone

from botapp.affect import classify_multi, context, evolve, normalize, zero_vector
from botapp.models import NoyaAffectProfile, NoyaPersonaFact, TelegramUser

NOYA_MENTION_RE = re.compile(r"(?<!\w)نویا(?!\w)|(?<!\w)@(?:nuyarobot|nuya|noya)(?!\w)", re.I)
PERSONA_HINT_RE = re.compile(
    r"سن|چند سال|تولد|متولد|تحصیل|رشته|دانشگاه|خاطره|کودکی|"
    r"گذشته|تروما|دراما|درام|قهر|دلتنگی|رفیق|دوستی|علاقه|"
    r"بارون|باران|تفریح|سرگرمی|معرفی|کی هستی|اسمت|ارزش|باور"
)


def addressed_to_noya(message) -> bool:
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    if not chat or not user or getattr(user, "is_bot", False):
        return False
    if getattr(chat, "type", "") == "private":
        # Only a private message actually delivered to this bot qualifies.
        return True
    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if NOYA_MENTION_RE.search(text):
        return True
    # Other bots and private third-party conversations are NOT authenticated
    # as messages directed to Noya.
    replied = getattr(message, "reply_to_message", None)
    bot = getattr(message, "bot", None)
    bot_id = getattr(bot, "id", None)
    original = getattr(replied, "from_user", None)
    return bool(replied and original and bot_id is not None
                and str(getattr(original, "id", "")) == str(bot_id))


@transaction.atomic
def observe_affect(message) -> dict | None:
    """Update global scores exactly once per Telegram message, never on neutral."""
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not user or not chat or getattr(user, "is_bot", False):
        return None
    if not addressed_to_noya(message):
        return None
    if getattr(message, "forward_origin", None):
        return None
    user_id = getattr(user, "id", None)
    message_id = getattr(message, "message_id", None)
    if user_id is None or message_id is None:
        return None
    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if not text.strip():
        return None
    # Most messages are neutral: avoid creating a profile or writing DB rows.
    # The one adult-gated cue can only be evaluated after loading a profile.
    prelim = classify_multi(text, addressed=True, adult_confirmed=False)
    norm = normalize(text)
    gated_cue = "برام جذابی" in norm or "از نظر عاطفی جذبتم" in norm
    if not prelim and not gated_cue:
        return None
    person, _ = TelegramUser.objects.get_or_create(telegram_user_id=int(user_id))
    profile, _ = NoyaAffectProfile.objects.get_or_create(user=person)
    profile = NoyaAffectProfile.objects.select_for_update().get(pk=profile.pk)
    signals = classify_multi(text, addressed=True, adult_confirmed=profile.adult_confirmed)
    if not signals:
        # Strict neutral NO-OP: scores, timestamps, event IDs unchanged.
        return context(profile.scores, adult_confirmed=profile.adult_confirmed)
    event_key = f"telegram:{int(chat.id)}:{int(message_id)}"
    if event_key in (profile.processed_message_keys or []):
        return context(profile.scores, adult_confirmed=profile.adult_confirmed)
    now = timezone.now()
    elapsed = 0.
    if profile.last_affective_at is not None:
        elapsed = max(0., (now - profile.last_affective_at).total_seconds() / 3600.)
    # Only verified update authors and directly addressed messages can update.
    profile.scores = evolve(profile.scores, signals, hours_since_previous=elapsed)
    profile.last_affective_at = now
    profile.processed_message_keys = (
        [*list(profile.processed_message_keys or []), event_key][-128:]
    )
    profile.save(update_fields=["scores", "last_affective_at",
                                "processed_message_keys", "updated_at"])
    return context(profile.scores, adult_confirmed=profile.adult_confirmed)


def get_affect_context(user_id: int) -> dict:
    profile = NoyaAffectProfile.objects.filter(user__telegram_user_id=int(user_id)).first()
    return context(profile.scores if profile else zero_vector(),
                   adult_confirmed=bool(profile and profile.adult_confirmed))


def fetch_persona_facts(question: str) -> list[dict]:
    """Retrieve only directly relevant *fictional* lore, not a giant prompt."""
    q = normalize(question)[:1200]
    if not q or not PERSONA_HINT_RE.search(q):
        return []
    hits = []
    for fact in NoyaPersonaFact.objects.filter(enabled=True).only(
        "key", "triggers", "value", "fictional"
    ):
        if not fact.fictional:
            continue
        count = sum(1 for trigger in (fact.triggers or [])
                    if isinstance(trigger, str) and normalize(trigger) in q)
        if count:
            hits.append((count, fact))
    hits.sort(key=lambda item: (-item[0], item[1].key))
    return [{"key": fact.key, "fictional": True, "story": fact.value[:800]}
            for _, fact in hits[:3]]


def delete_person_affect(user_id: int) -> int:
    """Forget the global emotion vector across chats for this user."""
    deleted, _ = NoyaAffectProfile.objects.filter(
        user__telegram_user_id=int(user_id)
    ).delete()
    return deleted
