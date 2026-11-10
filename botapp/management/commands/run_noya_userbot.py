"""Run Noya's personal Telegram *user* session, independently of Bot API."""
from __future__ import annotations

import asyncio
import logging

from django.core.management.base import BaseCommand, CommandError

from botapp.noya_userbot import NoyaUserbot, UserbotConfig

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run Noya on an already-authorized personal MTProto user session."

    def handle(self, *args, **options):
        try:
            config = UserbotConfig.from_env()
            path = config.session_path.with_suffix(".session")
            if not path.is_file():
                raise CommandError(
                    "User session is not authorized. First run "
                    "'manage.py login_noya_userbot' interactively."
                )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        async def runner():
            try:
                from telethon import TelegramClient, events
            except ImportError as exc:
                raise CommandError("Telethon is not installed") from exc
            # Never accept BOT_TOKEN here and never call client.start() on an
            # unattended server: this service only opens an existing session.
            client = TelegramClient(
                str(config.session_path), config.api_id, config.api_hash,
                auto_reconnect=True, sequential_updates=False,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise CommandError(
                        "Session is not authorized; login_noya_userbot must "
                        "be run explicitly by the account holder."
                    )
                me = await client.get_me()
                if getattr(me, "bot", False):
                    raise CommandError(
                        "Provided session belongs to a BotFather bot; "
                        "Noya userbot requires a personal Telegram account."
                    )
                runtime = NoyaUserbot(
                    client, config, owner_id=int(me.id),
                    username=str(getattr(me, "username", "") or ""),
                )
                client.add_event_handler(runtime.on_message, events.NewMessage)
                self.stdout.write(
                    f"Noya user session active id={me.id}, "
                    f"allowlisted_dialogs={len(config.allowed_chat_ids)}, "
                    f"saved_messages={'on' if config.saved_messages_enabled else 'off'}"
                )
                await client.run_until_disconnected()
            finally:
                await client.disconnect()

        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            self.stdout.write("Noya user session stopped.")
