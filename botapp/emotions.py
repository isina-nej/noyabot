"""Small, serializable relationship-state engine for Noya.

The values are interaction mechanics, not claims about human feelings.  The
module is deliberately independent from Django so persistence can reuse the
existing memory/conversation scopes without a second storage system.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from typing import Mapping

MIN_SCORE = 0.0
MAX_SCORE = 100.0
MAX_EVENT_IDS = 128

EVENT_DEFAULT_SEVERITY: dict[str, float] = {
    "friendly_message": 0.0,
    "compliment": 0.0,
    "gratitude": 0.0,
    "shared_humor": 0.0,
    "playful_teasing": 0.0,
    "mild_disrespect": 12.0,
    "serious_disrespect": 30.0,
    "repeated_disrespect": 45.0,
    "sincere_apology": 0.0,
    "acknowledgement_of_harm": 0.0,
    "behavioral_improvement": 0.0,
    "reconciliation": 0.0,
    "positive_news": 0.0,
    "disappointment": 10.0,
    "ordinary_message": 0.0,
}

REPAIR_DEFAULTS: dict[str, float] = {
    "sincere_apology": 25.0,
    "acknowledgement_of_harm": 20.0,
    "behavioral_improvement": 25.0,
    "reconciliation": 10.0,
}


def clamp(value: float, low: float = MIN_SCORE, high: float = MAX_SCORE) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    if not math.isfinite(number):
        number = low
    return max(low, min(high, number))


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class EmotionConfig:
    """Tunable thresholds/rates, kept separate for deterministic tests."""

    mild_hurt_max: float = 14.0
    sulky_hurt_max: float = 34.0
    noticeable_hurt_max: float = 59.0
    serious_hurt_max: float = 79.0
    minor_decay_per_day: float = 4.0
    serious_decay_per_day: float = 1.0


@dataclass(frozen=True)
class EmotionalEvent:
    kind: str
    event_id: str | None = None
    severity: float | None = None
    repair_points: float | None = None
    confidence: float = 1.0
    timestamp: datetime | None = None


@dataclass(frozen=True)
class EmotionalState:
    hurt_score: float = 0.0
    repair_progress: float = 0.0
    warmth: float = 55.0
    playfulness: float = 55.0
    trust_context: float = 50.0
    current_social_mood: str = "neutral"
    last_interaction_at: datetime | None = None
    last_negative_event_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    active_conflict_id: str | None = None
    processed_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def remaining_hurt(self) -> float:
        return clamp(self.hurt_score - self.repair_progress)

    @property
    def reconciled(self) -> bool:
        return self.remaining_hurt <= MIN_SCORE

    def to_dict(self) -> dict:
        """Return JSON-friendly data for an existing persistence layer."""
        return {
            "hurt_score": self.hurt_score,
            "repair_progress": self.repair_progress,
            "warmth": self.warmth,
            "playfulness": self.playfulness,
            "trust_context": self.trust_context,
            "current_social_mood": self.current_social_mood,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
            "last_negative_event_at": self.last_negative_event_at.isoformat() if self.last_negative_event_at else None,
            "last_reconciliation_at": self.last_reconciliation_at.isoformat() if self.last_reconciliation_at else None,
            "active_conflict_id": self.active_conflict_id,
            "processed_event_ids": list(self.processed_event_ids),
            "remaining_hurt": self.remaining_hurt,
        }

    @classmethod
    def from_dict(cls, raw: Mapping | None) -> "EmotionalState":
        raw = raw or {}

        def parsed_time(key: str):
            value = raw.get(key)
            if not value:
                return None
            try:
                return _now(datetime.fromisoformat(str(value)))
            except (TypeError, ValueError):
                return None

        return cls(
            hurt_score=clamp(raw.get("hurt_score", 0)),
            repair_progress=clamp(raw.get("repair_progress", 0), 0, clamp(raw.get("hurt_score", 0))),
            warmth=clamp(raw.get("warmth", 55)),
            playfulness=clamp(raw.get("playfulness", 55)),
            trust_context=clamp(raw.get("trust_context", 50)),
            current_social_mood=str(raw.get("current_social_mood") or "neutral"),
            last_interaction_at=parsed_time("last_interaction_at"),
            last_negative_event_at=parsed_time("last_negative_event_at"),
            last_reconciliation_at=parsed_time("last_reconciliation_at"),
            active_conflict_id=raw.get("active_conflict_id") or None,
            processed_event_ids=tuple(str(item) for item in (raw.get("processed_event_ids") or [])[-MAX_EVENT_IDS:]),
        )


def _event(value: EmotionalEvent | str, *, event_id: str | None, severity: float | None, repair_points: float | None, confidence: float) -> EmotionalEvent:
    if isinstance(value, EmotionalEvent):
        return value
    return EmotionalEvent(value, event_id=event_id, severity=severity, repair_points=repair_points, confidence=confidence)


def _remember(state: EmotionalState, event_id: str | None) -> EmotionalState:
    if not event_id:
        return state
    ids = tuple((*state.processed_event_ids, event_id)[-MAX_EVENT_IDS:])
    return replace(state, processed_event_ids=ids)


def _mood(remaining_hurt: float, *, config: EmotionConfig) -> str:
    if remaining_hurt <= config.mild_hurt_max:
        return "reconciled" if remaining_hurt == 0 else "mild_sulking"
    if remaining_hurt <= config.sulky_hurt_max:
        return "sulking"
    if remaining_hurt <= config.noticeable_hurt_max:
        return "hurt"
    if remaining_hurt <= config.serious_hurt_max:
        return "seriously_hurt"
    return "guarded"


def _with_mood(state: EmotionalState, *, config: EmotionConfig) -> EmotionalState:
    return replace(state, current_social_mood=_mood(state.remaining_hurt, config=config))


def apply_event(
    state: EmotionalState,
    event: EmotionalEvent | str,
    *,
    event_id: str | None = None,
    severity: float | None = None,
    repair_points: float | None = None,
    confidence: float = 1.0,
    now: datetime | None = None,
    config: EmotionConfig = EmotionConfig(),
) -> EmotionalState:
    """Apply one deduplicated social event and return the next state."""
    item = _event(event, event_id=event_id, severity=severity, repair_points=repair_points, confidence=confidence)
    if item.event_id and item.event_id in state.processed_event_ids:
        return state
    timestamp = _now(item.timestamp or now)
    confidence_value = clamp(item.confidence, 0, 1)
    kind = item.kind.strip().casefold()
    default_negative = EVENT_DEFAULT_SEVERITY.get(kind, 0.0)
    negative = max(0.0, item.severity if item.severity is not None and default_negative > 0 else default_negative)
    repair = max(0.0, item.repair_points if item.repair_points is not None else REPAIR_DEFAULTS.get(kind, 0.0))
    next_state = replace(state, last_interaction_at=timestamp)
    if negative:
        # Once a conflict is fully reconciled, a new incident starts a fresh
        # repair ledger instead of inheriting credit from the old conflict.
        if state.reconciled:
            next_state = replace(next_state, repair_progress=0.0)
        next_hurt = clamp(state.hurt_score + negative * confidence_value)
        conflict_id = item.event_id or state.active_conflict_id or f"conflict:{timestamp.timestamp():.6f}"
        next_state = replace(
            next_state,
            hurt_score=next_hurt,
            warmth=clamp(state.warmth - negative * 0.35),
            playfulness=clamp(state.playfulness - negative * 0.25),
            trust_context=clamp(state.trust_context - negative * 0.2),
            last_negative_event_at=timestamp,
            active_conflict_id=conflict_id,
        )
    elif repair:
        next_repair = clamp(state.repair_progress + repair * confidence_value, 0, next_state.hurt_score)
        next_state = replace(
            next_state,
            repair_progress=next_repair,
            warmth=clamp(state.warmth + repair * 0.2),
            playfulness=clamp(state.playfulness + repair * 0.12),
            trust_context=clamp(state.trust_context + repair * 0.08),
            last_reconciliation_at=timestamp if next_repair >= next_state.hurt_score else state.last_reconciliation_at,
            active_conflict_id=None if next_repair >= next_state.hurt_score else state.active_conflict_id,
        )
    elif kind in {"friendly_message", "compliment", "gratitude", "shared_humor", "positive_news"}:
        next_state = replace(
            next_state,
            warmth=clamp(state.warmth + (3.0 if kind == "compliment" else 1.5)),
            playfulness=clamp(state.playfulness + (3.0 if kind in {"shared_humor", "positive_news"} else 1.0)),
        )
    next_state = _remember(next_state, item.event_id)
    return _with_mood(next_state, config=config)


def decay_state(state: EmotionalState, *, now: datetime | None = None, days: float | None = None, config: EmotionConfig = EmotionConfig()) -> EmotionalState:
    """Apply bounded natural decay to hurt; major conflicts fade more slowly."""
    current = _now(now)
    if days is None:
        if state.last_negative_event_at is None:
            return replace(state, last_interaction_at=current)
        days = max(0.0, (current - _now(state.last_negative_event_at)).total_seconds() / 86400)
    days = max(0.0, float(days))
    if days == 0 or state.hurt_score <= 0:
        return _with_mood(replace(state, last_interaction_at=current), config=config)
    rate = config.serious_decay_per_day if state.hurt_score > config.sulky_hurt_max else config.minor_decay_per_day
    hurt = clamp(state.hurt_score - days * max(0.0, rate))
    repair = min(state.repair_progress, hurt)
    next_state = replace(
        state,
        hurt_score=hurt,
        repair_progress=repair,
        last_interaction_at=current,
        active_conflict_id=None if hurt == 0 else state.active_conflict_id,
    )
    return _with_mood(next_state, config=config)
