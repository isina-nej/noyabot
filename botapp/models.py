import secrets

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

class TelegramUser(models.Model):
    telegram_user_id = models.BigIntegerField(unique=True)
    started = models.BooleanField(default=False)
    blocked = models.BooleanField(default=False)
    last_live_check_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return str(self.telegram_user_id)

class BotMessageSettings(models.Model):
    start_message = models.TextField(default="ربات استارت شد!")
    join_channel_message = models.TextField(
        default="{mention} برای ارسال پیام باید در {channel} عضو شوید."
    )
    collaboration_enabled = models.BooleanField(default=True)
    channel_join_required = models.BooleanField(default=True)
    bot_start_required = models.BooleanField(default=True)

    def __str__(self):
        return f"BotMessageSettings({self.pk})"

class GroupQuota(models.Model):
    """Daily AI request quota per group.

    ``daily_prompt_limit=0`` means unlimited (no daily cap).
    """

    chat_id = models.BigIntegerField(unique=True)
    chat_title = models.CharField(max_length=255, blank=True, default="")
    daily_prompt_limit = models.PositiveIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Daily AI prompts allowed for this group. 0 = unlimited.",
    )
    tokens_used_today = models.PositiveIntegerField(default=0)
    last_reset = models.DateField(default=timezone.localdate)

    def __str__(self):
        if self.daily_prompt_limit == 0:
            return f"GroupQuota({self.chat_id}) - {self.tokens_used_today}/∞"
        return f"GroupQuota({self.chat_id}) - {self.tokens_used_today}/{self.daily_prompt_limit}"

def generate_token() -> str:
    return secrets.token_urlsafe(32)

class ChatLink(models.Model):
    token = models.CharField(max_length=64, unique=True, default=generate_token, editable=False)
    group_chat_id = models.BigIntegerField()
    group_title = models.CharField(max_length=255, blank=True, default="")
    created_by_user_id = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def is_valid(self) -> bool:
        return self.is_active and (
            self.expires_at is None or timezone.now() <= self.expires_at
        )

    def __str__(self):
        return f"ChatLink({self.token[:8]}...) -> {self.group_chat_id}"

class GroupSettings(models.Model):
    chat_id = models.BigIntegerField(unique=True)
    chat_title = models.CharField(max_length=255, blank=True, default="")
    channel_join_required = models.BooleanField(default=True)
    bot_start_required = models.BooleanField(default=True)
    collaboration_enabled = models.BooleanField(default=True)
    mandatory_channel = models.CharField(max_length=100, blank=True, default="")
    group_admins = models.JSONField(default=list, blank=True)

    moderation_enabled = models.BooleanField(default=True)
    anti_spam_enabled = models.BooleanField(default=True)
    anti_link_enabled = models.BooleanField(default=True)
    anti_forward_enabled = models.BooleanField(default=False)
    welcome_enabled = models.BooleanField(default=True)
    goodbye_enabled = models.BooleanField(default=False)
    rules_enabled = models.BooleanField(default=True)
    captcha_enabled = models.BooleanField(default=False)
    log_retention_days = models.PositiveIntegerField(default=90)
    flood_limit = models.PositiveIntegerField(default=6)
    flood_window_seconds = models.PositiveIntegerField(default=10)
    duplicate_limit = models.PositiveIntegerField(default=3)
    mute_duration_minutes = models.PositiveIntegerField(default=10)
    max_warnings = models.PositiveIntegerField(default=3)
    max_warnings_action = models.CharField(
        max_length=10,
        default="mute",
        choices=[("mute", "Mute"), ("ban", "Ban")],
    )
    max_warnings_action_delay_minutes = models.PositiveIntegerField(default=0)
    max_warnings_action_duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=10,
        validators=[MinValueValidator(1)],
    )
    warning_expiry_days = models.PositiveIntegerField(default=30)
    message_templates = models.JSONField(default=dict, blank=True)
    open_permissions_snapshot = models.JSONField(default=dict, blank=True)
    welcome_message = models.TextField(
        default=(
            "سلام #name عزیز به گروه #title خوش آمدی 🌷 "
            "✅ ساعت: ( #time ) ✅ تاریخ: ( #date )"
        )
    )
    goodbye_message = models.TextField(default="{name} از گروه خارج شد.")
    rules_text = models.TextField(default="قوانین گروه هنوز تنظیم نشده است.")
    blocked_words = models.JSONField(default=list, blank=True)
    allowed_domains = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    welcome_emoji = models.ForeignKey(
        "CustomEmoji",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="welcomed_in_groups",
        help_text="Custom emoji to use in welcome messages for this group.",
    )

    def __str__(self):
        return f"GroupSettings({self.chat_id}) - {self.chat_title}"

class GroupActionLog(models.Model):
    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    admin_user_id = models.BigIntegerField()
    admin_name = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=100)
    old_value = models.BooleanField(null=True)
    new_value = models.BooleanField(null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"Log({self.group.chat_id}) {self.action} by {self.admin_user_id}"

class Warning(models.Model):
    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="warnings",
    )
    user_id = models.BigIntegerField()
    user_name = models.CharField(max_length=255, blank=True)
    issued_by_user_id = models.BigIntegerField()
    issued_by_name = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["group", "user_id", "-created_at"])]

    @property
    def is_active(self):
        return self.revoked_at is None and (
            self.expires_at is None or timezone.now() < self.expires_at
        )

class ModerationAction(models.Model):
    ACTIONS = (
        ("lock", "Lock group"),
        ("unlock", "Unlock group"),
        ("mute", "Mute user"),
        ("unmute", "Unmute user"),
        ("ban", "Ban user"),
        ("unban", "Unban user"),
    )
    STATUSES = (
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("executed", "Executed"),
        ("cancelled", "Cancelled"),
        ("failed", "Failed"),
    )

    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="actions",
    )
    action = models.CharField(max_length=10, choices=ACTIONS)
    target_user_id = models.BigIntegerField(null=True, blank=True)
    target_name = models.CharField(max_length=255, blank=True)
    actor_user_id = models.BigIntegerField(null=True, blank=True)
    actor_name = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    execute_at = models.DateTimeField(default=timezone.now, db_index=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default="pending", db_index=True)
    idempotency_key = models.CharField(max_length=80, unique=True)
    source = models.CharField(max_length=20, default="telegram")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["execute_at"]
        indexes = [
            models.Index(fields=["status", "execute_at"]),
            models.Index(fields=["group", "target_user_id", "status"]),
        ]

class GroupSchedule(models.Model):
    ACTIONS = (("lock", "Lock"), ("unlock", "Unlock"))

    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="schedules",
    )
    action = models.CharField(max_length=10, choices=ACTIONS)
    time_of_day = models.TimeField()
    is_active = models.BooleanField(default=True)
    last_enqueued_date = models.DateField(null=True, blank=True)
    created_by_user_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "action", "time_of_day"],
                name="unique_group_daily_schedule",
            )
        ]

class StaffAPIKey(models.Model):
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, db_index=True)
    key_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    # Empty list = unrestricted (legacy single-tenant keys). When non-empty,
    # the REST API only allows the listed Telegram chat_id values.
    allowed_chat_ids = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_bot_api_keys",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def allows_chat(self, chat_id: int) -> bool:
        allowed = self.allowed_chat_ids or []
        if not allowed:
            return True
        try:
            target = int(chat_id)
        except (TypeError, ValueError):
            return False
        for value in allowed:
            try:
                if int(value) == target:
                    return True
            except (TypeError, ValueError):
                continue
        return False

class ModerationLog(models.Model):
    ACTIONS = (
        ("warn", "Warn"),
        ("unwarn", "Unwarn"),
        ("lock", "Lock"),
        ("unlock", "Unlock"),
        ("mute", "Mute"),
        ("unmute", "Unmute"),
        ("ban", "Ban"),
        ("unban", "Unban"),
        ("delete", "Delete"),
        ("filter", "Filter"),
    )

    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="moderation_logs",
    )
    target_user_id = models.BigIntegerField(null=True, blank=True)
    target_name = models.CharField(max_length=255, blank=True)
    actor_user_id = models.BigIntegerField(null=True, blank=True)
    actor_name = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=20, choices=ACTIONS)
    reason = models.CharField(max_length=500, blank=True)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    moderation_action = models.ForeignKey(
        ModerationAction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["group", "-created_at"])]

class GroupUserGateState(models.Model):
    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="user_gate_states",
    )
    telegram_user_id = models.BigIntegerField()
    first_message_at = models.DateTimeField(null=True, blank=True)
    warning_pending_at = models.DateTimeField(null=True, blank=True)
    warning_sent_at = models.DateTimeField(null=True, blank=True)
    last_warning_update_at = models.DateTimeField(null=True, blank=True)
    warning_message_id = models.BigIntegerField(null=True, blank=True)
    notice_delete_at = models.DateTimeField(null=True, blank=True)
    message_deleted_count = models.PositiveBigIntegerField(default=0)
    last_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "telegram_user_id"],
                name="unique_group_user_gate_state",
            )
        ]
        indexes = [models.Index(fields=["group", "telegram_user_id"])]

class BotStartGateEvent(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="bot_start_gate_events",
    )
    telegram_user_id = models.BigIntegerField()
    message_id = models.BigIntegerField()
    telegram_update_id = models.BigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "message_id"],
                name="uniq_bot_start_event_group_message",
            )
        ]
        indexes = [models.Index(fields=["group", "telegram_user_id", "status"])]

class ForcedMembershipRule(models.Model):
    source_group = models.ForeignKey(
        GroupSettings,
        on_delete=models.CASCADE,
        related_name="forced_membership_rules",
    )
    destination_chat_id = models.BigIntegerField()
    destination_chat_title = models.CharField(max_length=255, blank=True, default="")
    destination_chat_type = models.CharField(max_length=20, blank=True, default="")
    destination_username = models.CharField(max_length=32, blank=True, default="")
    destination_join_url = models.URLField(max_length=500, blank=True, default="")
    created_by_user_id = models.BigIntegerField()
    is_active = models.BooleanField(default=True)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    bot_source_permissions_valid = models.BooleanField(default=False)
    bot_destination_permissions_valid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_group", "destination_chat_id"],
                name="uniq_forced_rule_source_destination",
            )
        ]
        indexes = [
            models.Index(fields=["source_group", "is_active"]),
            models.Index(fields=["destination_chat_id", "is_active"]),
        ]

    def __str__(self):
        return f"{self.source_group.chat_id} -> {self.destination_chat_id}"

class ForcedMembershipUserState(models.Model):
    rule = models.ForeignKey(
        ForcedMembershipRule,
        on_delete=models.CASCADE,
        related_name="user_states",
    )
    telegram_user_id = models.BigIntegerField()
    username = models.CharField(max_length=32, blank=True, default="")
    first_name = models.CharField(max_length=255, blank=True, default="")
    last_name = models.CharField(max_length=255, blank=True, default="")
    current_membership_status = models.CharField(max_length=32, default="unknown")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    first_warning_at = models.DateTimeField(null=True, blank=True)
    last_warning_at = models.DateTimeField(null=True, blank=True)
    first_joined_at = models.DateTimeField(null=True, blank=True)
    last_joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    message_count_while_not_member = models.PositiveBigIntegerField(default=0)
    deleted_message_count = models.PositiveBigIntegerField(default=0)
    join_count = models.PositiveIntegerField(default=0)
    leave_count = models.PositiveIntegerField(default=0)
    rejoin_count = models.PositiveIntegerField(default=0)
    has_ever_joined = models.BooleanField(default=False)
    is_currently_member = models.BooleanField(default=False)
    joined_via_tracked_invite = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "telegram_user_id"],
                name="uniq_forced_state_rule_user",
            )
        ]
        indexes = [
            models.Index(fields=["rule", "current_membership_status"]),
            models.Index(fields=["rule", "has_ever_joined"]),
            models.Index(fields=["rule", "is_currently_member"]),
        ]

class ForcedMembershipInviteLink(models.Model):
    rule = models.ForeignKey(
        ForcedMembershipRule,
        on_delete=models.CASCADE,
        related_name="invite_links",
    )
    telegram_user_id = models.BigIntegerField()
    invite_link = models.URLField(max_length=500, unique=True)
    telegram_invite_link_name = models.CharField(max_length=100, blank=True, default="")
    expire_at = models.DateTimeField(null=True, blank=True)
    member_limit = models.PositiveIntegerField(null=True, blank=True)
    is_revoked = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["rule", "telegram_user_id", "is_revoked"])]

class ForcedMembershipEvent(models.Model):
    rule = models.ForeignKey(
        ForcedMembershipRule,
        on_delete=models.CASCADE,
        related_name="events",
    )
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    event_type = models.CharField(max_length=40)
    source_message_id = models.BigIntegerField(null=True, blank=True)
    telegram_update_id = models.BigIntegerField(null=True, blank=True)
    old_status = models.CharField(max_length=32, blank=True, default="")
    new_status = models.CharField(max_length=32, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "telegram_update_id"],
                name="uniq_forced_event_rule_update",
            )
        ]
        indexes = [
            models.Index(fields=["rule", "event_type", "created_at"]),
            models.Index(fields=["telegram_update_id"]),
        ]

class MemoryConversation(models.Model):
    platform = models.CharField(max_length=32, default="telegram")
    chat_id = models.BigIntegerField()
    thread_id = models.BigIntegerField(default=0)
    conversation_id = models.CharField(max_length=128, blank=True, default="")
    chat_type = models.CharField(max_length=32, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "chat_id", "thread_id", "conversation_id"],
                name="unique_memory_conversation_identity",
            )
        ]

        indexes = [
            models.Index(fields=["platform", "chat_id"]),
            models.Index(fields=["last_activity_at"]),
        ]

    def __str__(self):
        return f"{self.platform}:{self.chat_id}:{self.thread_id}:{self.conversation_id}"

class MemoryConversationMember(models.Model):
    conversation = models.ForeignKey(
        MemoryConversation,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="memory_memberships",
    )
    role = models.CharField(max_length=32, blank=True, default="member")
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "user"],
                name="unique_memory_conversation_member",
            )
        ]
        indexes = [
            models.Index(fields=["conversation", "last_activity_at"]),
            models.Index(fields=["user", "last_activity_at"]),
        ]


class NoyaRelationshipState(models.Model):
    """Per-user, per-conversation social state for Noya's persona."""

    user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="noya_relationship_states",
    )
    conversation = models.ForeignKey(
        MemoryConversation,
        on_delete=models.CASCADE,
        related_name="noya_relationship_states",
    )
    hurt_score = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    repair_progress = models.PositiveSmallIntegerField(
        default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    warmth = models.PositiveSmallIntegerField(
        default=55, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    playfulness = models.PositiveSmallIntegerField(
        default=55, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    trust_context = models.PositiveSmallIntegerField(
        default=50, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    current_social_mood = models.CharField(max_length=32, default="neutral")
    # Explicit, database-authoritative phase of the relationship: the language
    # model may vary wording but never contradicts this value.
    relationship_status = models.CharField(
        max_length=16,
        default="neutral",
        choices=[
            ("neutral", "Neutral"),
            ("hurt", "Hurt"),
            ("repairing", "Repairing"),
            ("reconciled", "Reconciled"),
        ],
    )
    active_conflict_id = models.CharField(max_length=128, blank=True, default="")
    processed_event_ids = models.JSONField(default=list, blank=True)
    # Per-conflict repair credits (``<conflict id>|<credit class>``) so repeated
    # apologies inside one conflict cannot keep buying forgiveness.
    repair_credits = models.JSONField(default=list, blank=True)
    reconciliation_count = models.PositiveSmallIntegerField(default=0)
    last_interaction_at = models.DateTimeField(null=True, blank=True)
    last_negative_event_at = models.DateTimeField(null=True, blank=True)
    last_reconciliation_at = models.DateTimeField(null=True, blank=True)
    conflict_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "conversation"],
                name="unique_noya_relationship_user_conversation",
            )
        ]
        indexes = [
            models.Index(fields=["user", "updated_at"]),
            models.Index(fields=["conversation", "updated_at"]),
        ]


class NoyaRelationshipEvent(models.Model):
    """Deduplicated social events feeding a relationship state."""

    state = models.ForeignKey(
        NoyaRelationshipState,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=64)
    severity = models.FloatField(default=0)
    confidence = models.FloatField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["state", "event_id"],
                name="unique_noya_relationship_event",
            )
        ]
        indexes = [
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["created_at"]),
        ]

class MemoryItem(models.Model):
    class Scope(models.TextChoices):
        USER = "user", "User global"
        CONVERSATION = "conversation", "Conversation"
        USER_CONVERSATION = "user_conversation", "User conversation"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        CONVERSATION_ONLY = "conversation_only", "Conversation only"
        USER_GLOBAL = "user_global", "User global"
        GROUP_SHARED = "group_shared", "Group shared"
        SYSTEM_ONLY = "system_only", "System only"

    class Importance(models.TextChoices):
        CRITICAL = "critical", "Critical"
        IMPORTANT = "important", "Important"
        MODERATELY_IMPORTANT = "moderately_important", "Moderately important"
        UNIMPORTANT = "unimportant", "Unimportant"

    class Retention(models.TextChoices):
        SHORT = "short", "Short"
        MEDIUM = "medium", "Medium"
        LONG = "long", "Long"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        SUPERSEDED = "superseded", "Superseded"
        DELETED = "deleted", "Deleted"
        ARCHIVED = "archived", "Archived"
        PENDING_REVIEW = "pending_review", "Pending review"

    class Category(models.TextChoices):
        PREFERENCE = "preference", "Preference"
        IDENTITY = "identity", "Identity"
        PROJECT = "project", "Project"
        DECISION = "decision", "Decision"
        RELATIONSHIP = "relationship", "Relationship"
        GOAL = "goal", "Goal"
        CONSTRAINT = "constraint", "Constraint"
        EVENT = "event", "Event"
        OTHER = "other", "Other"

    owner_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="memory_items",
    )
    subject_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subject_memory_items",
    )
    conversation = models.ForeignKey(
        MemoryConversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_items",
    )
    memory_scope = models.CharField(max_length=24, choices=Scope.choices)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.OTHER)
    content = models.TextField()
    normalized_content = models.TextField()
    importance = models.CharField(max_length=24, choices=Importance.choices)
    retention_level = models.CharField(max_length=16, choices=Retention.choices)
    visibility = models.CharField(max_length=24, choices=Visibility.choices)
    confidence = models.FloatField(default=0.0)
    is_explicit = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    last_confirmed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    mention_count = models.PositiveIntegerField(default=1)
    access_count = models.PositiveIntegerField(default=0)
    source_count = models.PositiveIntegerField(default=0)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superseded_items",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["owner_user", "status", "visibility"]),
            models.Index(fields=["conversation", "status", "visibility"]),
            models.Index(fields=["retention_level", "expires_at"]),
            models.Index(fields=["category", "importance"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0, confidence__lte=1),
                name="memory_item_confidence_between_zero_one",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        memory_scope="user",
                        conversation__isnull=True,
                        visibility="user_global",
                    )
                    | (
                        models.Q(
                            memory_scope__in=["conversation", "user_conversation"],
                            conversation__isnull=False,
                        )
                        & ~models.Q(visibility="user_global")
                    )
                ),
                name="memory_scope_visibility_consistent",
            ),
        ]

class MemorySource(models.Model):
    memory = models.ForeignKey(
        MemoryItem,
        on_delete=models.CASCADE,
        related_name="sources",
    )
    speaker_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_sources",
    )
    platform = models.CharField(max_length=32, default="telegram")
    source_chat_id = models.BigIntegerField(null=True, blank=True)
    source_message_id = models.BigIntegerField(null=True, blank=True)
    source_thread_id = models.BigIntegerField(default=0)
    source_kind = models.CharField(max_length=32, default="message")
    occurred_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["memory", "platform", "source_chat_id", "source_message_id"],
                name="unique_memory_source_message",
            )
        ]
        indexes = [
            models.Index(fields=["source_chat_id", "source_message_id"]),
            models.Index(fields=["speaker_user", "occurred_at"]),
        ]

class MemoryLifecycleEvent(models.Model):
    memory = models.ForeignKey(
        MemoryItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lifecycle_events",
    )
    event_type = models.CharField(max_length=32)
    old_status = models.CharField(max_length=16, blank=True, default="")
    new_status = models.CharField(max_length=16, blank=True, default="")
    reason = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    actor_user = models.ForeignKey(
        TelegramUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memory_lifecycle_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "created_at"]),
            models.Index(fields=["memory", "created_at"]),
        ]


class CustomEmoji(models.Model):
    name = models.CharField(
        max_length=50, unique=True, help_text="A unique name for the emoji, e.g., 'fire_animated'"
    )
    custom_emoji_id = models.CharField(
        max_length=100, unique=True, help_text="The actual custom emoji ID from Telegram"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.custom_emoji_id})"

    class Meta:
        verbose_name = "Custom Emoji"
        verbose_name_plural = "Custom Emojis"
        ordering = ["name"]


class ChatDailyActivity(models.Model):
    """Lightweight per-day activity counters the bot observes in a chat.

    Always maintained (independent of full MessageSnapshot archival). Counts only
    messages the bot actually receives after the feature is enabled — Telegram
    Bot API cannot backfill older history.
    """

    chat_id = models.BigIntegerField(db_index=True)
    day = models.DateField(db_index=True)
    message_count = models.PositiveIntegerField(default=0)
    media_count = models.PositiveIntegerField(default=0)
    unique_sender_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chat_id", "day"], name="uniq_chat_daily_activity"),
        ]
        indexes = [models.Index(fields=["chat_id", "-day"])]
        ordering = ["-day"]

    def __str__(self):
        return f"ChatDailyActivity({self.chat_id}:{self.day}={self.message_count})"


class ChatDailySender(models.Model):
    """Per-sender message counts for a chat/day (supports top-poster analytics)."""

    chat_id = models.BigIntegerField(db_index=True)
    day = models.DateField(db_index=True)
    user_id = models.BigIntegerField()
    display_name = models.CharField(max_length=255, blank=True, default="")
    username = models.CharField(max_length=64, blank=True, default="")
    message_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chat_id", "day", "user_id"],
                name="uniq_chat_daily_sender",
            )
        ]
        indexes = [models.Index(fields=["chat_id", "day", "-message_count"])]


class MessageSnapshot(models.Model):
    """Archive of messages the bot itself received/deleted in a group.

    Telegram does not give bots a reliable public event for *manual* message
    deletions in ordinary groups, so this only ever records messages the bot
    observed and (optionally) deletions the bot itself performed.
    """

    chat_id = models.BigIntegerField(db_index=True)
    message_id = models.BigIntegerField()
    thread_id = models.BigIntegerField(default=0)
    sender_user_id = models.BigIntegerField(null=True, blank=True)
    sender_chat_id = models.BigIntegerField(null=True, blank=True)
    sender_name = models.CharField(max_length=255, blank=True, default="")
    sender_username = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField(blank=True, default="")
    caption = models.TextField(blank=True, default="")
    content_type = models.CharField(max_length=32, blank=True, default="text")
    reply_to_message_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(auto_now_add=True)
    deleted_by_bot_at = models.DateTimeField(null=True, blank=True)
    deletion_reason = models.CharField(max_length=255, blank=True, default="")
    deletion_action = models.ForeignKey(
        ModerationAction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="message_snapshots",
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chat_id", "message_id"],
                name="uniq_message_snapshot_chat_message",
            )
        ]
        indexes = [
            models.Index(fields=["chat_id", "-archived_at"]),
            models.Index(fields=["chat_id", "deleted_by_bot_at"]),
        ]
        ordering = ["-archived_at"]

    def __str__(self):
        return f"MessageSnapshot({self.chat_id}:{self.message_id})"


class AgentConfirmation(models.Model):
    """A pending high/medium-risk operation awaiting a glass-key confirmation."""

    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_CANCELLED = "cancelled"
    STATUS_EXPIRED = "expired"
    STATUS_EXECUTING = "executing"
    STATUS_EXECUTED = "executed"
    STATUS_FAILED = "failed"
    STATUSES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_EXECUTING, "Executing"),
        (STATUS_EXECUTED, "Executed"),
        (STATUS_FAILED, "Failed"),
    )

    token_hash = models.CharField(max_length=64, unique=True)
    # Operational chat (group/channel) where the tool runs.
    chat_id = models.BigIntegerField(db_index=True)
    # Chat where the confirm/cancel keyboard was shown. Differs from chat_id when
    # an admin drives the agent from a private chat against a remote target.
    request_chat_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    request_message_id = models.BigIntegerField(null=True, blank=True)
    confirmation_message_id = models.BigIntegerField(null=True, blank=True)
    requester_user_id = models.BigIntegerField()
    requester_name = models.CharField(max_length=255, blank=True, default="")
    tool_name = models.CharField(max_length=64)
    target_user_id = models.BigIntegerField(null=True, blank=True)
    validated_parameters = models.JSONField(default=dict, blank=True)
    human_summary = models.CharField(max_length=300, blank=True, default="")
    risk_level = models.CharField(max_length=8, default="medium")
    status = models.CharField(max_length=12, choices=STATUSES, default=STATUS_PENDING, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Set when the row transitions to "executing"; used to detect records left
    # stuck in "executing" if the process died mid-execution (recovery policy).
    executing_started_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    result_summary = models.CharField(max_length=500, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["chat_id", "requester_user_id", "status"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"AgentConfirmation({self.pk}:{self.tool_name}:{self.status})"


class AgentAuditLog(models.Model):
    """Structured audit trail for every admin-agent request.

    Sensitive material (tokens, keys, secrets, full prompts) is never stored.
    """

    request_id = models.CharField(max_length=36, db_index=True)
    chat_id = models.BigIntegerField(db_index=True)
    requester_user_id = models.BigIntegerField()
    requester_role = models.CharField(max_length=16, blank=True, default="")
    original_command = models.CharField(max_length=2000, blank=True, default="")
    parser_type = models.CharField(max_length=16, blank=True, default="")
    detected_intent = models.CharField(max_length=64, blank=True, default="")
    tool_name = models.CharField(max_length=64, blank=True, default="")
    sanitized_parameters = models.JSONField(default=dict, blank=True)
    risk_level = models.CharField(max_length=8, blank=True, default="")
    confirmation_status = models.CharField(max_length=16, blank=True, default="")
    execution_status = models.CharField(max_length=16, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["chat_id", "-created_at"]),
            models.Index(fields=["tool_name", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"AgentAuditLog({self.request_id}:{self.tool_name})"
