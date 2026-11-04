"""Read-only summary of the persisted emotional-memory state.

Never prints user messages, API credentials, bios, or profile fields.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from botapp.models import NoyaRelationshipState


class Command(BaseCommand):
    help = "Inspect Noya emotional-memory persistence without modifying data."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, help="Telegram numeric user ID")
        parser.add_argument("--chat-id", type=int, help="Optional Telegram chat/group ID")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        states = NoyaRelationshipState.objects.select_related("conversation", "user")
        if options["user_id"] is not None:
            states = states.filter(user__telegram_user_id=options["user_id"])
        if options["chat_id"] is not None:
            states = states.filter(conversation__chat_id=options["chat_id"])

        summary = dict(states.values_list("relationship_status").annotate(count=Count("pk")))
        self.stdout.write("Relationship state counts: " + str(summary))
        self.stdout.write("Matching records: " + str(states.count()))

        for row in states.order_by("-updated_at")[:max(1, min(100, options["limit"]))]:
            self.stdout.write(
                f"user={row.user.telegram_user_id} chat={row.conversation.chat_id} "
                f"thread={row.conversation.thread_id} status={row.relationship_status} "
                f"hurt={row.hurt_score} repair={row.repair_progress} "
                f"warmth={row.warmth} trust={row.trust_context} "
                f"events={row.events.count()} updated_at={row.updated_at.isoformat()}"
            )

        if not states.exists():
            self.stdout.write(
                "No emotional-state rows in this scope. "
                "Events are recorded when a user addresses Noya through the AI chat path; "
                "other users, groups and direct messages have separate state."
            )
