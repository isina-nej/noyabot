"""One-time interactive authorization of a personal MTProto session.

Never pass the login code/password or a session string to the bot, LLM,
GitHub, logs or environment. Run in a trusted interactive shell with 0600
session storage and a dedicated session file, not the read-only channel
analysis session that another service might already hold open.
"""
from __future__ import annotations

import asyncio
import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from botapp.noya_userbot import UserbotConfig


class Command(BaseCommand):
    help = "Interactively authorize NOYA_USERBOT_SESSION_PATH once (not a Bot API token)."

    def handle(self, *args, **options):
        if not os.isatty(0):
            raise CommandError("Interactive terminal required; never store login codes in env.")
        try:
            config = UserbotConfig.from_env()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        directory = config.session_path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            raise CommandError("Session directory must have chmod 700")
        old_umask = os.umask(0o077)

        async def authorize():
            from telethon import TelegramClient
            client = TelegramClient(
                str(config.session_path), config.api_id, config.api_hash,
            )
            try:
                await client.start(
                    phone=lambda: input("Telegram phone number (+country...): ").strip(),
                    code_callback=lambda: getpass.getpass("One-time Telegram code: "),
                    password=lambda: getpass.getpass("Telegram 2FA password (if required): "),
                )
                user = await client.get_me()
                if not user or getattr(user, "bot", False):
                    raise CommandError("Expected a personal Telegram user account, not a bot.")
                self.stdout.write(f"Authorized account id={user.id}. Session saved locally.")
            finally:
                await client.disconnect()

        try:
            asyncio.run(authorize())
            session_file = config.session_path.with_suffix(".session")
            if session_file.exists():
                session_file.chmod(0o600)
        finally:
            os.umask(old_umask)
