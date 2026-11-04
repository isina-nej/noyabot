"""Explicit, dry-run-first import of unattributed legacy group snapshots.

Legacy MessageSnapshot rows have NO bot_id. A human operator must select
which bot actually observed each source group. Never infer ownership or
automatically copy private messages between bots.
"""
from django.core.management.base import BaseCommand, CommandError

from botapp.models import BotInboxMessage, MessageSnapshot


class Command(BaseCommand):
    help = "Dry-run/apply migration of a known group's historic snapshots to one verified bot id."

    def add_arguments(self, parser):
        parser.add_argument("--bot-id", type=int, required=True)
        parser.add_argument("--chat-id", type=int, required=True)
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        bot_id, chat_id = opts["bot_id"], opts["chat_id"]
        if bot_id <= 0 or chat_id >= 0:
            raise CommandError("Only positive bot IDs and negative group chat IDs are supported.")
        limit = opts["limit"]
        if not 1 <= limit <= 10000:
            raise CommandError("limit must be in 1..10000")
        queryset = MessageSnapshot.objects.filter(chat_id=chat_id).order_by("message_id")
        snapshots = list(queryset[:limit])
        count = len(snapshots)
        self.stdout.write(
            f"Legacy snapshots matching group={chat_id}: {count}. "
            "Source contains no bot identity; verify this group belongs to the selected bot."
        )
        if not opts["apply"]:
            self.stdout.write("DRY RUN: nothing was imported. Pass --apply after verifying provenance.")
            return

        chat_name = str(chat_id)
        records = [
            BotInboxMessage(
                bot_id=bot_id, chat_id=chat_id, message_id=m.message_id,
                chat_type="supergroup", chat_title=chat_name,
                thread_id=m.thread_id, sender_user_id=m.sender_user_id,
                sender_name=m.sender_name, sender_username=m.sender_username,
                text=m.text, caption=m.caption, content_type=m.content_type,
                reply_to_message_id=m.reply_to_message_id,
                message_date=m.created_at,
            )
            for m in snapshots
        ]
        BotInboxMessage.objects.bulk_create(records, ignore_conflicts=True, batch_size=300)
        self.stdout.write(
            f"Import attempted: {count}; duplicates were skipped. "
            "Use the inbox to inspect the resulting chat."
        )
