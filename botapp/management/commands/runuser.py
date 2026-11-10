"""Run Noya as an explicitly authorized Telegram *user* session via Telethon."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from botapp.user_session import NoyaUserSession, UserSessionConfig

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run Noya on a personal Telegram account (Telethon), separate from runbot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--login", action="store_true",
            help="Interactively authorize a new user session on a trusted terminal.",
        )

    def handle(self, *args, **options):
        try:
            config = UserSessionConfig.from_environment()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        try:
            asyncio.run(self._run(config, login=options["login"]))
        except KeyboardInterrupt:
            self.stdout.write("User-session stopped.")
        except (OSError, RuntimeError, ValueError) as exc:
            raise CommandError(f"User-session startup failed: {type(exc).__name__}") from exc

    async def _run(self, config: UserSessionConfig, *, login: bool) -> None:
        from telethon import TelegramClient, events

        session_path = Path(config.session)
        session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        session_file = Path(str(session_path) + ".session")
        if session_file.exists():
            session_file.chmod(0o600)
        old_mask = os.umask(0o077)
        try:
            client = TelegramClient(config.session, config.api_id, config.api_hash)
            await client.connect()
        finally:
            os.umask(old_mask)

        try:
            if not await client.is_user_authorized():
                if not login:
                    raise CommandError(
                        "Telegram session is not authorized. On a trusted interactive "
                        "terminal run: python manage.py runuser --login"
                    )
                await client.start()
            me = await client.get_me()
            if me is None or getattr(me, "bot", False):
                raise CommandError("TELEGRAM_API_ID/HASH session must belong to a user, not a bot.")
            if session_file.exists():
                session_file.chmod(0o600)
            runner = NoyaUserSession(
                client, config, int(me.id), getattr(me, "username", "") or "",
            )
            client.add_event_handler(runner.handle, events.NewMessage(incoming=True))
            self.stdout.write(
                self.style.SUCCESS(
                    "Noya user-session connected. "
                    f"Chat allowlist: {len(config.allowed_chat_ids)} chats; "
                    f"private replies: {config.private_replies}; "
                    f"AI agent tools: {config.agent_enabled}."
                )
            )
            await client.run_until_disconnected()
        finally:
            await client.disconnect()
