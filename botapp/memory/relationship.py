"""Persistent, scoped relationship state for Noya.

This is an adapter around :mod:`botapp.emotions`; it deliberately reuses the
existing Telegram user/conversation identities instead of creating a second
memory graph.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Mapping

from django.db import transaction
from django.conf import settings
from django.utils import timezone

from botapp.emotions import EmotionConfig, EmotionalEvent, EmotionalState, apply_event, decay_state
from botapp.models import (
    MemoryConversation,
    NoyaRelationshipEvent,
    NoyaRelationshipState,
    TelegramUser,
)


def _state_from_row(row: NoyaRelationshipState) -> EmotionalState:
    return EmotionalState(
        hurt_score=row.hurt_score,
        repair_progress=row.repair_progress,
        warmth=row.warmth,
        playfulness=row.playfulness,
        trust_context=row.trust_context,
        current_social_mood=row.current_social_mood,
        last_interaction_at=row.last_interaction_at,
        last_negative_event_at=row.last_negative_event_at,
        last_reconciliation_at=row.last_reconciliation_at,
        active_conflict_id=row.active_conflict_id or None,
        processed_event_ids=tuple(str(value) for value in (row.processed_event_ids or [])[-128:]),
    )


def _save_state(row: NoyaRelationshipState, state: EmotionalState) -> None:
    data = state.to_dict()
    row.hurt_score = round(data["hurt_score"])
    row.repair_progress = round(data["repair_progress"])
    row.warmth = round(data["warmth"])
    row.playfulness = round(data["playfulness"])
    row.trust_context = round(data["trust_context"])
    row.current_social_mood = data["current_social_mood"]
    row.active_conflict_id = data["active_conflict_id"] or ""
    row.processed_event_ids = data["processed_event_ids"]
    row.last_interaction_at = state.last_interaction_at
    row.last_negative_event_at = state.last_negative_event_at
    row.last_reconciliation_at = state.last_reconciliation_at
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
    item = event if isinstance(event, EmotionalEvent) else EmotionalEvent(
        kind=event,
        event_id=event_id,
        severity=severity,
        confidence=confidence,
        repair_points=repair_points,
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


def relationship_context(state: EmotionalState | NoyaRelationshipState) -> dict:
    """Return bounded model-facing context without event history or raw IDs."""
    if isinstance(state, NoyaRelationshipState):
        state = _state_from_row(state)
    return {
        "current_social_mood": state.current_social_mood,
        "hurt_score": round(state.hurt_score),
        "repair_progress": round(state.repair_progress),
        "remaining_hurt": round(state.remaining_hurt),
        "warmth": round(state.warmth),
        "playfulness": round(state.playfulness),
        "trust_context": round(state.trust_context),
        "has_active_conflict": bool(state.active_conflict_id),
        "last_interaction_at": state.last_interaction_at.isoformat() if state.last_interaction_at else None,
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
