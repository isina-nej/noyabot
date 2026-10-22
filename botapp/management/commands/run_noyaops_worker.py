"""Dedicated private operations runner: no diagnostics are executed in HTTP requests.

Launch separately from the Telegram polling worker and the Django web worker:
    python manage.py run_noyaops_worker
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand

from botapp.observability.diagnostics import execute_next

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the isolated, allowlisted remote-diagnostics queue worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one queued run and exit")
        parser.add_argument("--poll-seconds", type=float, default=1.0)

    def handle(self, *args, **options):
        pause = max(0.25, min(float(options["poll_seconds"]), 30.0))
        try:
            while True:
                try:
                    worked = execute_next()
                except Exception as exc:
                    logger.warning("noyaops.worker.failure error_type=%s", type(exc).__name__)
                    worked = False
                if options["once"]:
                    return
                if not worked:
                    time.sleep(pause)
        except KeyboardInterrupt:
            return
