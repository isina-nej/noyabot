"""Purge expired bot-inbox message archives after a bounded retention period."""
from django.core.management.base import BaseCommand

from botapp.bot_inbox import purge_expired


class Command(BaseCommand):
    help = "Remove bot-inbox messages older than BOT_INBOX_RETENTION_DAYS."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)

    def handle(self, *args, **options):
        days = options["days"]
        if days is not None and not (1 <= days <= 365):
            raise ValueError("--days must be between 1 and 365")
        count = purge_expired(days=days)
        self.stdout.write(f"Deleted {count} expired inbox messages.")
