"""Purge archived message snapshots older than the configured retention."""

from django.conf import settings
from django.core.management.base import BaseCommand

from botapp.message_archive import purge_old


class Command(BaseCommand):
    help = "Delete MessageSnapshot rows older than MESSAGE_ARCHIVE_RETENTION_DAYS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--retention-days",
            type=int,
            default=None,
            help="Override MESSAGE_ARCHIVE_RETENTION_DAYS for this run.",
        )

    def handle(self, *args, **options):
        retention = options["retention_days"] or int(
            getattr(settings, "MESSAGE_ARCHIVE_RETENTION_DAYS", 30)
        )
        deleted = purge_old(retention)
        from botapp.noya_identity import purge_stale_identities
        identities = purge_stale_identities(retention)
        self.stdout.write(f"Deleted {deleted} message snapshot(s) older than {retention} day(s).")
        self.stdout.write(f"Deleted {identities} inactive observed identity row(s).")
