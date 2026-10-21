"""Small, serializable relationship-state engine for Noya.

The values are interaction mechanics, not claims about human feelings.  The
module is deliberately independent from Django so persistence can reuse the
existing memory/conversation scopes without a second storage system.

The engine owns an explicit, deterministic state machine::

    neutral -> hurt -> repairing -> reconciled

* ``neutral``    no conflict was ever recorded.
* ``hurt``       an active conflict exists and no repair credit was used yet.
* ``repairing``  an active conflict exists and at least one repair credit was
                 used, but remaining hurt is still above zero.
* ``reconciled`` the last conflict is closed and remaining hurt is zero.

A new negative event after ``reconciled`` starts a *fresh* conflict ledger, so
old hurt can never accumulate into a new incident.  Repair credits are capped
per conflict and per credit class, so repeated apologies cannot buy repeated
forgiveness.  Everything is derived from persisted primitives, which keeps the
database authoritative and the language model unable to invent transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from typing import Mapping

MIN_SCORE = 0.0
MAX_SCORE = 100.0
MAX_EVENT_IDS = 128
MAX_REPAIR_CREDITS = 32

RELATIONSHIP_STATUSES = frozenset({"neutral", "hurt", "repairing", "reconciled"})
ACTIVE_CONFLICT_STATUSES = frozenset({"hurt", "repairing"})
RECONCILED_STATUS = "reconciled"
NEUTRAL_STATUS = "neutral"

EVENT_DEFAULT_SEVERITY: dict[str, float] = {
    "friendly_message": 0.0,
    "compliment": 0.0,
    "gratitude": 0.0,
    "shared_humor": 0.0,
    "playful_teasing": 0.0,
    "normal_disagreement": 0.0,
    "quality_criticism": 0.0,
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

# Credit classes: at most one credit per (conflict, class).  Three apologies in
# the same conflict therefore grant exactly one credit and at most one
# forgiveness event.
REPAIR_CREDITS: dict[str, str] = {
    "sincere_apology": "apology",
    "acknowledgement_of_harm": "acknowledgement",
    "behavioral_improvement": "behavior",
    "reconciliation": "reconciliation",
}

POSITIVE_KINDS = frozenset(
    {"friendly_message", "compliment", "gratitude", "shared_humor", "positive_news"}
)


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
    relationship_status: str = NEUTRAL_STATUS
    last_interaction_at: datetime | None = None
    last_negative_event_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    conflict_started_at: datetime | None = None
    active_conflict_id: str | None = None
    reconciliation_count: int = 0
    repair_credits: tuple[str, ...] = field(default_factory=tuple)
    processed_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def remaining_hurt(self) -> float:
        return clamp(self.hurt_score - self.repair_progress)

    @property
    def reconciled(self) -> bool:
        return self.remaining_hurt <= MIN_SCORE

    @property
    def conflict_active(self) -> bool:
        return self.relationship_status in ACTIVE_CONFLICT_STATUSES

    def to_dict(self) -> dict:
        """Return JSON-friendly data for an existing persistence layer."""
        return {
            "hurt_score": self.hurt_score,
            "repair_progress": self.repair_progress,
            "warmth": self.warmth,
            "playfulness": self.playfulness,
            "trust_context": self.trust_context,
            "current_social_mood": self.current_social_mood,
            "relationship_status": self.relationship_status,
            "last_interaction_at": self.last_interaction_at.isoformat() if self.last_interaction_at else None,
            "last_negative_event_at": self.last_negative_event_at.isoformat() if self.last_negative_event_at else None,
            "last_reconciliation_at": self.last_reconciliation_at.isoformat() if self.last_reconciliation_at else None,
            "conflict_started_at": self.conflict_started_at.isoformat() if self.conflict_started_at else None,
            "active_conflict_id": self.active_conflict_id,
            "reconciliation_count": self.reconciliation_count,
            "repair_credits": list(self.repair_credits),
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

        hurt = clamp(raw.get("hurt_score", 0))
        status = str(raw.get("relationship_status") or NEUTRAL_STATUS)
        return cls(
            hurt_score=hurt,
            repair_progress=clamp(raw.get("repair_progress", 0), 0, hurt),
            warmth=clamp(raw.get("warmth", 55)),
            playfulness=clamp(raw.get("playfulness", 55)),
            trust_context=clamp(raw.get("trust_context", 50)),
            current_social_mood=str(raw.get("current_social_mood") or "neutral"),
            relationship_status=status if status in RELATIONSHIP_STATUSES else NEUTRAL_STATUS,
            last_interaction_at=parsed_time("last_interaction_at"),
            last_negative_event_at=parsed_time("last_negative_event_at"),
            last_reconciliation_at=parsed_time("last_reconciliation_at"),
            conflict_started_at=parsed_time("conflict_started_at"),
            active_conflict_id=raw.get("active_conflict_id") or None,
            reconciliation_count=max(0, _safe_int(raw.get("reconciliation_count"))),
            repair_credits=tuple(
                str(item) for item in (raw.get("repair_credits") or [])[-MAX_REPAIR_CREDITS:]
            ),
            processed_event_ids=tuple(str(item) for item in (raw.get("processed_event_ids") or [])[-MAX_EVENT_IDS:]),
        )


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def coerce_event(
    value: EmotionalEvent | str | object,
    *,
    event_id: str | None = None,
    severity: float | None = None,
    repair_points: float | None = None,
    confidence: float = 1.0,
    timestamp: datetime | None = None,
) -> EmotionalEvent:
    """Normalize any event-like input into this module's ``EmotionalEvent``.

    Duck typing (instead of a strict ``isinstance``) keeps the engine correct
    when an event object was created by another module version — e.g. a
    hot-reloaded process — where a strict class check would silently treat the
    object as a plain kind string.
    """
    if isinstance(value, EmotionalEvent):
        return value
    kind = getattr(value, "kind", None)
    if kind is not None:
        return EmotionalEvent(
            str(kind),
            event_id=getattr(value, "event_id", None) or event_id,
            severity=getattr(value, "severity", None) if getattr(value, "severity", None) is not None else severity,
            repair_points=(
                getattr(value, "repair_points", None)
                if getattr(value, "repair_points", None) is not None
                else repair_points
            ),
            confidence=getattr(value, "confidence", None) if getattr(value, "confidence", None) is not None else confidence,
            timestamp=getattr(value, "timestamp", None) or timestamp,
        )
    return EmotionalEvent(
        str(value),
        event_id=event_id,
        severity=severity,
        repair_points=repair_points,
        confidence=confidence,
        timestamp=timestamp,
    )


def _event(value: EmotionalEvent | str, *, event_id: str | None, severity: float | None, repair_points: float | None, confidence: float) -> EmotionalEvent:
    return coerce_event(
        value,
        event_id=event_id,
        severity=severity,
        repair_points=repair_points,
        confidence=confidence,
    )


def _remember(state: EmotionalState, event_id: str | None) -> EmotionalState:
    if not event_id:
        return state
    ids = tuple((*state.processed_event_ids, event_id)[-MAX_EVENT_IDS:])
    return replace(state, processed_event_ids=ids)


def derive_relationship_status(
    *,
    hurt_score: float,
    repair_progress: float,
    active_conflict_id: str | None,
    repair_credits: tuple[str, ...] | list[str],
    reconciliation_count: int,
) -> str:
    """Single source of truth for the transition table (DB stays authoritative)."""
    remaining = clamp(hurt_score - repair_progress)
    if remaining <= MIN_SCORE:
        if reconciliation_count > 0 or active_conflict_id:
            return RECONCILED_STATUS
        return NEUTRAL_STATUS
    if repair_credits:
        return "repairing"
    return "hurt"


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


def _finalize(state: EmotionalState, *, config: EmotionConfig) -> EmotionalState:
    """Recompute mood + status from primitives so they can never disagree."""
    status = derive_relationship_status(
        hurt_score=state.hurt_score,
        repair_progress=state.repair_progress,
        active_conflict_id=state.active_conflict_id,
        repair_credits=state.repair_credits,
        reconciliation_count=state.reconciliation_count,
    )
    active_conflict_id = state.active_conflict_id
    if status == RECONCILED_STATUS:
        active_conflict_id = None
    return replace(
        state,
        current_social_mood=_mood(state.remaining_hurt, config=config),
        relationship_status=status,
        active_conflict_id=active_conflict_id,
    )


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
    kind = item.kind.strip().casefold()
    if item.event_id and item.event_id in state.processed_event_ids:
        return state
    timestamp = _now(item.timestamp or now)
    confidence_value = clamp(item.confidence, 0, 1)
    default_negative = EVENT_DEFAULT_SEVERITY.get(kind, 0.0)
    negative = max(0.0, item.severity if item.severity is not None and default_negative > 0 else default_negative)
    repair = max(0.0, item.repair_points if item.repair_points is not None else REPAIR_DEFAULTS.get(kind, 0.0))
    next_state = replace(state, last_interaction_at=timestamp)

    if negative:
        # Every genuine new negative event opens a NEW conflict incident.  Only
        # the residual (unrepaired) hurt carries over, so a forgiven conflict can
        # never inflate the next one, and each incident gets its own repair
        # budget.  Repair credit from a previous incident is never inherited.
        carried_hurt = state.remaining_hurt
        conflict_id = item.event_id or f"conflict:{timestamp.timestamp():.6f}"
        next_state = replace(
            next_state,
            hurt_score=clamp(carried_hurt + negative * confidence_value),
            repair_progress=0.0,
            warmth=clamp(state.warmth - negative * 0.35),
            playfulness=clamp(state.playfulness - negative * 0.25),
            trust_context=clamp(state.trust_context - negative * 0.2),
            last_negative_event_at=timestamp,
            conflict_started_at=timestamp,
            active_conflict_id=conflict_id,
            repair_credits=(),
        )
    elif repair and kind in REPAIR_CREDITS and state.conflict_active and state.remaining_hurt > MIN_SCORE:
        credit_key = f"{state.active_conflict_id or 'none'}|{REPAIR_CREDITS[kind]}"
        if credit_key in state.repair_credits:
            # Repeated apology inside the SAME conflict: no points, no new
            # forgiveness event, no state transition.
            next_state = replace(next_state, current_social_mood=state.current_social_mood)
        else:
            next_repair = clamp(state.repair_progress + repair * confidence_value, 0, state.hurt_score)
            fully_reconciled = state.hurt_score > MIN_SCORE and next_repair >= state.hurt_score
            next_state = replace(
                next_state,
                repair_progress=next_repair,
                warmth=clamp(state.warmth + repair * 0.2),
                playfulness=clamp(state.playfulness + repair * 0.12),
                trust_context=clamp(state.trust_context + repair * 0.08),
                repair_credits=tuple((*state.repair_credits, credit_key)[-MAX_REPAIR_CREDITS:]),
                last_reconciliation_at=timestamp if fully_reconciled else state.last_reconciliation_at,
                active_conflict_id=None if fully_reconciled else state.active_conflict_id,
                reconciliation_count=state.reconciliation_count + (1 if fully_reconciled else 0),
            )
    elif kind in POSITIVE_KINDS:
        next_state = replace(
            next_state,
            warmth=clamp(state.warmth + (3.0 if kind == "compliment" else 1.5)),
            playfulness=clamp(state.playfulness + (3.0 if kind in {"shared_humor", "positive_news"} else 1.0)),
        )
    next_state = _remember(next_state, item.event_id)
    return _finalize(next_state, config=config)


def decay_state(state: EmotionalState, *, now: datetime | None = None, days: float | None = None, config: EmotionConfig = EmotionConfig()) -> EmotionalState:
    """Apply bounded natural decay to hurt; major conflicts fade more slowly."""
    current = _now(now)
    if days is None:
        if state.last_negative_event_at is None:
            return _finalize(replace(state, last_interaction_at=current), config=config)
        days = max(0.0, (current - _now(state.last_negative_event_at)).total_seconds() / 86400)
    days = max(0.0, float(days))
    if days == 0 or state.hurt_score <= 0:
        return _finalize(replace(state, last_interaction_at=current), config=config)
    rate = config.serious_decay_per_day if state.hurt_score > config.sulky_hurt_max else config.minor_decay_per_day
    hurt = clamp(state.hurt_score - days * max(0.0, rate))
    repair = min(state.repair_progress, hurt)
    became_reconciled = hurt <= MIN_SCORE and state.remaining_hurt > MIN_SCORE and state.conflict_active
    next_state = replace(
        state,
        hurt_score=hurt,
        repair_progress=repair,
        last_interaction_at=current,
        active_conflict_id=None if hurt <= MIN_SCORE else state.active_conflict_id,
        reconciliation_count=state.reconciliation_count + (1 if became_reconciled else 0),
        last_reconciliation_at=current if became_reconciled else state.last_reconciliation_at,
    )
    return _finalize(next_state, config=config)
