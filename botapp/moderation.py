import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from botapp.models import GroupSchedule, GroupSettings, ModerationAction, StaffAPIKey

ACTION_TYPES = {"lock", "unlock", "mute", "unmute", "ban", "unban"}
TARGET_REQUIRED = {"mute", "unmute", "ban", "unban"}
REVERSALS = {"lock": "unlock", "mute": "unmute", "ban": "unban"}

# Idempotency-key namespaces. Internal keys (auto-reversals, daily schedules)
# live under "sys:" and are reserved. Keys coming from untrusted callers are
# forced under "ext:" so they can never collide with — and thus pre-book /
# poison — a reserved internal key (e.g. blocking an automatic unmute/unban or a
# daily lock by pre-creating a row with that key).
SYSTEM_KEY_PREFIX = "sys:"
EXTERNAL_KEY_PREFIX = "ext:"
_INTERNAL_SOURCES = {"system", "schedule"}
_KEY_MAX_LENGTH = 80

DEFAULT_MESSAGES = {
    "warn": "به {target} اخطار داده شد ({count}/{max_warnings}).",
    "warning_ceiling": "{target} به سقف اخطار رسید؛ {action} برای او ثبت شد.",
    "lock": "گروه قفل شد.",
    "unlock": "قفل گروه باز شد.",
    "mute": "{target} سکوت شد.",
    "unmute": "سکوت {target} برداشته شد.",
    "ban": "{target} بن شد.",
    "unban": "بن {target} برداشته شد.",
    "scheduled": "{action} برای {execute_at} زمان‌بندی شد.",
    "admin_protected": "{target} ادمین گروه است و نمی‌توان او را مجازات کرد.",
}


@dataclass(frozen=True)
class ActionSpec:
    action: str
    delay_minutes: int = 0
    duration_minutes: int | None = None

    @property
    def is_delayed(self):
        return self.delay_minutes > 0

    @property
    def is_timed(self):
        return self.duration_minutes is not None


def parse_positive_minutes(value, field, allow_none=True):
    if value in (None, "") and allow_none:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


def validate_action_spec(action, delay_minutes=0, duration_minutes=None):
    if action not in ACTION_TYPES:
        raise ValueError("unsupported action")
    delay = 0 if delay_minutes in (None, "", 0, "0") else parse_positive_minutes(delay_minutes, "delay_minutes", False)
    duration = parse_positive_minutes(duration_minutes, "duration_minutes")
    if action in {"unlock", "unmute", "unban"} and duration is not None:
        raise ValueError("release actions cannot have a duration")
    return ActionSpec(action=action, delay_minutes=delay, duration_minutes=duration)


def render_group_message(group, key, **context):
    template = group.message_templates.get(key) or DEFAULT_MESSAGES[key]
    try:
        return template.format(**context)
    except (KeyError, ValueError, IndexError):
        return DEFAULT_MESSAGES[key].format(**context)


@transaction.atomic
def create_action(
    *,
    group,
    action,
    target_user_id=None,
    target_name="",
    actor_user_id=None,
    actor_name="",
    reason="",
    delay_minutes=0,
    duration_minutes=None,
    idempotency_key=None,
    source="telegram",
):
    spec = validate_action_spec(action, delay_minutes, duration_minutes)
    if action in TARGET_REQUIRED and target_user_id is None:
        raise ValueError("target_user_id is required")
    if action in {"lock", "unlock"} and target_user_id is not None:
        raise ValueError("group actions cannot have target_user_id")

    if idempotency_key:
        if source in _INTERNAL_SOURCES:
            # Trusted internal callers already namespace their keys under "sys:".
            key = idempotency_key
        else:
            # Untrusted callers (API, telegram) can never reach the "sys:"
            # namespace, so they cannot pre-book a reserved reversal/schedule key.
            key = f"{EXTERNAL_KEY_PREFIX}{idempotency_key}"[:_KEY_MAX_LENGTH]
    else:
        key = uuid.uuid4().hex
    existing = ModerationAction.objects.filter(idempotency_key=key).first()
    if existing:
        return existing, False
    execute_at = timezone.now() + timedelta(minutes=spec.delay_minutes)
    try:
        created = ModerationAction.objects.create(
            group=group,
            action=spec.action,
            target_user_id=target_user_id,
            target_name=target_name,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
            reason=reason,
            execute_at=execute_at,
            duration_minutes=spec.duration_minutes,
            idempotency_key=key,
            source=source,
        )
    except IntegrityError:
        return ModerationAction.objects.get(idempotency_key=key), False
    return created, True


def warning_ceiling_spec(group):
    return validate_action_spec(
        group.max_warnings_action,
        group.max_warnings_action_delay_minutes,
        group.max_warnings_action_duration_minutes,
    )


@transaction.atomic
def claim_due_actions(limit=100, now=None):
    now = now or timezone.now()
    claimed = []
    queryset = (
        ModerationAction.objects.select_for_update()
        .filter(status="pending", execute_at__lte=now)
        .order_by("execute_at")[:limit]
    )
    for action in queryset:
        action.status = "processing"
        action.started_at = now
        action.save(update_fields=["status", "started_at"])
        claimed.append(action.id)
    return claimed


@transaction.atomic
def complete_action(action, executed_at=None):
    action = ModerationAction.objects.select_for_update().select_related("group").get(pk=action.pk)
    if action.status not in {"pending", "processing"}:
        raise ValueError("action is no longer executable")
    executed_at = executed_at or timezone.now()
    action.status = "executed"
    action.executed_at = executed_at
    action.error = ""
    action.save(update_fields=["status", "executed_at", "error"])
    reversal = create_reversal(action, executed_at)
    return action, reversal


def mark_action_executed(action, executed_at=None):
    return complete_action(action, executed_at)[0]


# Actions that apply a Telegram restriction. Auto-retrying these after a stale
# ``processing`` timeout can double-apply the side effect if the first worker
# already succeeded at Telegram but died before ``complete_action``.
_SIDE_EFFECT_ACTIONS = frozenset({"mute", "ban", "lock"})
# Release actions are safer to retry (idempotent-ish at Telegram).
_RELEASE_ACTIONS = frozenset({"unmute", "unban", "unlock"})


def requeue_stale_processing_actions(stale_minutes=10, now=None):
    """Recover stuck ``processing`` rows without double-applying restrictions.

    * ``mute`` / ``ban`` / ``lock`` → marked ``failed`` (manual review / re-issue).
    * ``unmute`` / ``unban`` / ``unlock`` → requeued to ``pending``.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=max(stale_minutes, 1))
    stale = ModerationAction.objects.filter(status="processing", started_at__lt=cutoff)
    failed = stale.filter(action__in=_SIDE_EFFECT_ACTIONS).update(
        status="failed",
        error="stale processing timeout; not auto-retried to avoid double Telegram side effects",
    )
    requeued = stale.filter(action__in=_RELEASE_ACTIONS).update(
        status="pending",
        started_at=None,
        error="requeued after stale processing timeout",
    )
    return failed + requeued


def mark_action_failed(action, error):
    action.status = "failed"
    action.error = str(error)[:2000]
    action.save(update_fields=["status", "error"])


@transaction.atomic
def cancel_action(action):
    """Cancel only queued actions with a conditional atomic transition.

    A worker that already claimed an action as 'processing' may have
    performed Telegram side effects: never promise a successful cancel then.
    """
    when = timezone.now()
    changed = ModerationAction.objects.filter(
        pk=action.pk, status="pending",
    ).update(status="cancelled", cancelled_at=when)
    if not changed:
        raise ValueError("only pending actions can be cancelled")
    action.status = "cancelled"
    action.cancelled_at = when
    return action


def create_reversal(action, executed_at=None):
    reverse_action = REVERSALS.get(action.action)
    if not reverse_action or action.duration_minutes is None:
        return None
    executed_at = executed_at or timezone.now()
    return create_action(
        group=action.group,
        action=reverse_action,
        target_user_id=action.target_user_id,
        target_name=action.target_name,
        actor_user_id=action.actor_user_id,
        actor_name=action.actor_name,
        reason=f"automatic release for action {action.id}",
        delay_minutes=action.duration_minutes,
        idempotency_key=f"{SYSTEM_KEY_PREFIX}reverse:{action.id}",
        source="system",
    )[0]


@transaction.atomic
def enqueue_daily_group_schedules(now=None):
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    enqueued = 0
    schedules = GroupSchedule.objects.select_for_update().select_related("group").filter(
        is_active=True,
        time_of_day__lte=now.time(),
    )
    for schedule in schedules:
        if schedule.last_enqueued_date == today:
            continue
        _, created = create_action(
            group=schedule.group,
            action=schedule.action,
            actor_user_id=schedule.created_by_user_id,
            reason="daily group schedule",
            idempotency_key=f"{SYSTEM_KEY_PREFIX}daily:{schedule.id}:{today.isoformat()}",
            source="schedule",
        )
        schedule.last_enqueued_date = today
        schedule.save(update_fields=["last_enqueued_date"])
        enqueued += int(created)
    return enqueued


def create_api_key(name, created_by=None):
    raw = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key = StaffAPIKey.objects.create(
        name=name,
        prefix=raw[:12],
        key_hash=digest,
        created_by=created_by,
    )
    return key, raw


def authenticate_api_key(raw):
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key = StaffAPIKey.objects.filter(key_hash=digest, is_active=True, revoked_at__isnull=True).first()
    if key:
        key.last_used_at = timezone.now()
        key.save(update_fields=["last_used_at"])
    return key
