"""Persistent, scoped relationship state for Noya.

This is an adapter around :mod:`botapp.emotions`; it deliberately reuses the
existing Telegram user/conversation identities instead of creating a second
memory graph.

The database is authoritative for the relationship: the persisted row holds the
hurt ledger, the active conflict, the per-conflict repair credits, the number of
closed (forgiven) conflicts and the explicit ``relationship_status``.  The
language model only receives a bounded, qualitative view of that state and is
never allowed to invent transitions that contradict it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from botapp.emotions import (
    EmotionConfig,
    EmotionalEvent,
    EmotionalState,
    apply_event,
    coerce_event,
    decay_state,
    derive_relationship_status,
)
from botapp.models import (
    MemoryConversation,
    NoyaRelationshipEvent,
    NoyaRelationshipState,
    TelegramUser,
)

# Deterministic, short, non-scripted tone directives for the model.
TONE_DIRECTIVES: dict[str, str] = {
    "neutral": (
        "دلخوری فعالی وجود ندارد؛ لحن عادی و گرم باشد و موضوع رابطه را پیش نکش."
    ),
    "hurt": (
        "یک دلخوری فعال هست؛ کمی کم‌گرم‌تر و محتاط‌تر باش، ولی کمک فنی، تصحیح خطا و "
        "پاسخ درست را کامل و بی‌کم‌وکاست بده."
    ),
    "repairing": (
        "دلجویی شروع شده ولی کامل نشده؛ گرم‌تر از حالت دلخوری باش، اما هنوز کدورت را "
        "کامل تمام‌شده اعلام نکن."
    ),
    "reconciled": (
        "رابطه آشتی‌شده و دلخوری فعال صفر است؛ دوباره عذرخواهی نخواه، کدورت را زنده "
        "نکن و اگر موقعیت اجازه داد می‌توانی شوخی گذشته بکنی."
    ),
}


def _state_from_row(row: NoyaRelationshipState) -> EmotionalState:
    hurt = float(row.hurt_score or 0)
    repair = float(row.repair_progress or 0)
    credits = tuple(str(value) for value in (getattr(row, "repair_credits", None) or []))
    state = EmotionalState(
        hurt_score=hurt,
        repair_progress=min(repair, hurt),
        warmth=row.warmth,
        playfulness=row.playfulness,
        trust_context=row.trust_context,
        current_social_mood=row.current_social_mood,
        relationship_status=str(getattr(row, "relationship_status", "") or "neutral"),
        last_interaction_at=row.last_interaction_at,
        last_negative_event_at=row.last_negative_event_at,
        last_reconciliation_at=row.last_reconciliation_at,
        conflict_started_at=getattr(row, "conflict_started_at", None),
        active_conflict_id=row.active_conflict_id or None,
        reconciliation_count=int(getattr(row, "reconciliation_count", 0) or 0),
        repair_credits=credits,
        processed_event_ids=tuple(str(value) for value in (row.processed_event_ids or [])[-128:]),
    )
    # The row is authoritative, but its status is always re-derived from the
    # persisted primitives so a stale/legacy value can never contradict them.
    return _with_status(state)


def _with_status(state: EmotionalState) -> EmotionalState:
    status = derive_relationship_status(
        hurt_score=state.hurt_score,
        repair_progress=state.repair_progress,
        active_conflict_id=state.active_conflict_id,
        repair_credits=state.repair_credits,
        reconciliation_count=state.reconciliation_count,
    )
    if status == state.relationship_status:
        return state
    from dataclasses import replace as _replace

    active = state.active_conflict_id
    if status == "reconciled":
        active = None
    return _replace(state, relationship_status=status, active_conflict_id=active)


def _save_state(row: NoyaRelationshipState, state: EmotionalState) -> None:
    data = state.to_dict()
    row.hurt_score = round(data["hurt_score"])
    row.repair_progress = min(round(data["repair_progress"]), row.hurt_score)
    row.warmth = round(data["warmth"])
    row.playfulness = round(data["playfulness"])
    row.trust_context = round(data["trust_context"])
    row.current_social_mood = data["current_social_mood"]
    row.active_conflict_id = data["active_conflict_id"] or ""
    row.processed_event_ids = data["processed_event_ids"]
    row.last_interaction_at = state.last_interaction_at
    row.last_negative_event_at = state.last_negative_event_at
    row.last_reconciliation_at = state.last_reconciliation_at
    if hasattr(row, "relationship_status"):
        row.conflict_started_at = state.conflict_started_at
        row.reconciliation_count = max(0, int(state.reconciliation_count))
        row.repair_credits = list(state.repair_credits)
        # Derive from the ROUNDED values that actually land in the database so a
        # reload can never observe a status the row itself contradicts.
        row.relationship_status = derive_relationship_status(
            hurt_score=row.hurt_score,
            repair_progress=row.repair_progress,
            active_conflict_id=row.active_conflict_id or None,
            repair_credits=row.repair_credits,
            reconciliation_count=row.reconciliation_count,
        )
    row.save()


def _emotion_config() -> EmotionConfig:
    """Read tunable thresholds/rates without changing the public API."""
    return EmotionConfig(
        mild_hurt_max=float(getattr(settings, "NOYA_HURT_MILD_MAX", 14)),
        sulky_hurt_max=float(getattr(settings, "NOYA_HURT_SULKY_MAX", 34)),
        noticeable_hurt_max=float(getattr(settings, "NOYA_HURT_NOTICEABLE_MAX", 59)),
        serious_hurt_max=float(getattr(settings, "NOYA_HURT_SERIOUS_MAX", 79)),
        minor_decay_per_day=float(getattr(settings, "NOYA_HURT_MINOR_DECAY_PER_DAY", 4)),
        serious_decay_per_day=float(getattr(settings, "NOYA_HURT_SERIOUS_DECAY_PER_DAY", 1)),
    )


def _event_retention_days() -> int:
    return max(1, int(getattr(settings, "NOYA_RELATIONSHIP_EVENT_RETENTION_DAYS", 90)))


def _prune_state_events(row: NoyaRelationshipState, now) -> None:
    cutoff = now - timedelta(days=_event_retention_days())
    row.events.filter(created_at__lt=cutoff).delete()


@transaction.atomic
def get_or_create_relationship_state(
    user_id: int,
    conversation: MemoryConversation,
) -> NoyaRelationshipState:
    user, _ = TelegramUser.objects.get_or_create(telegram_user_id=int(user_id))
    row, _ = NoyaRelationshipState.objects.get_or_create(user=user, conversation=conversation)
    return NoyaRelationshipState.objects.select_for_update().get(pk=row.pk)


@transaction.atomic
def apply_relationship_event(
    user_id: int,
    conversation: MemoryConversation,
    event: EmotionalEvent | str,
    *,
    event_id: str | None = None,
    severity: float | None = None,
    repair_points: float | None = None,
    confidence: float = 1.0,
    metadata: Mapping | None = None,
    now=None,
) -> EmotionalState:
    """Apply one event atomically and deduplicate by scoped ``event_id``."""
    row = get_or_create_relationship_state(user_id, conversation)
    timestamp = now or timezone.now()
    _prune_state_events(row, timestamp)
    config = _emotion_config()
    state = decay_state(_state_from_row(row), now=timestamp, config=config)
    item = coerce_event(
        event,
        event_id=event_id,
        severity=severity,
        repair_points=repair_points,
        confidence=confidence,
        timestamp=timestamp,
    )
    effective_event_id = event_id or item.event_id
    if effective_event_id and NoyaRelationshipEvent.objects.filter(
        state=row, event_id=effective_event_id
    ).exists():
        if state != _state_from_row(row):
            _save_state(row, state)
        return _state_from_row(row)
    next_state = apply_event(state, item, now=timestamp, config=config)
    if effective_event_id:
        NoyaRelationshipEvent.objects.create(
            state=row,
            event_id=effective_event_id,
            kind=item.kind,
            severity=float(item.severity or 0),
            confidence=float(item.confidence),
            metadata=dict(metadata or {}),
        )
    _save_state(row, next_state)
    return _state_from_row(row)


@transaction.atomic
def get_relationship_state(
    user_id: int,
    conversation: MemoryConversation,
    *,
    now=None,
) -> EmotionalState:
    row = get_or_create_relationship_state(user_id, conversation)
    timestamp = now or timezone.now()
    _prune_state_events(row, timestamp)
    state = decay_state(_state_from_row(row), now=timestamp, config=_emotion_config())
    _save_state(row, state)
    return _state_from_row(row)


def _level(value: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "medium"
    if number < 35:
        return "low"
    if number < 70:
        return "medium"
    return "high"


def relationship_context(state: EmotionalState | NoyaRelationshipState) -> dict:
    """Return the bounded, qualitative, authoritative model-facing context.

    Raw scores stay internal: the model receives the relationship phase, the
    permissions the phase implies and the claims that would contradict the
    persisted state.
    """
    if isinstance(state, NoyaRelationshipState):
        state = _state_from_row(state)
    status = state.relationship_status if state.relationship_status in TONE_DIRECTIVES else "neutral"
    remaining = state.remaining_hurt
    conflict_active = status in {"hurt", "repairing"} and remaining > 0
    reconciled = status == "reconciled"
    if conflict_active:
        do_not_claim = ["already_forgave", "no_conflict"]
    else:
        do_not_claim = ["still_upset", "waiting_for_apology"]
        if reconciled:
            do_not_claim.append("requires_new_apology")
    mild_max = _emotion_config().mild_hurt_max
    return {
        "authoritative": True,
        "relationship_status": status,
        "conflict_active": conflict_active,
        "conflict_is_reconciled": reconciled,
        "may_request_apology": bool(conflict_active and remaining > mild_max),
        "may_reference_past_conflict_playfully": bool(
            reconciled and state.reconciliation_count > 0
        ),
        "warmth_level": _level(state.warmth),
        "playfulness_level": _level(state.playfulness),
        "trust_level": _level(state.trust_context),
        "tone_directive": TONE_DIRECTIVES[status],
        "do_not_claim": do_not_claim,
    }


@transaction.atomic
def delete_relationship_state(user_id: int, conversation: MemoryConversation) -> int:
    queryset = NoyaRelationshipState.objects.filter(
        user__telegram_user_id=int(user_id), conversation=conversation
    )
    count = queryset.count()
    queryset.delete()
    return count


@transaction.atomic
def delete_all_relationship_states(user_id: int) -> int:
    """Delete every relationship ledger owned by one Telegram user."""
    queryset = NoyaRelationshipState.objects.filter(user__telegram_user_id=int(user_id))
    count = queryset.count()
    queryset.delete()
    return count


@transaction.atomic
def prune_relationship_events(*, max_age_days: int = 90, now=None) -> int:
    cutoff = (now or timezone.now()) - timedelta(days=max(1, int(max_age_days)))
    return NoyaRelationshipEvent.objects.filter(created_at__lt=cutoff).delete()[0]
