from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.management.commands.runbot import (
    MultiBotGroupDeduplicationMiddleware,
    parse_bot_tokens,
)


class MultiBotTests(SimpleTestCase):
    def message(self, *, chat_id=-1001, message_id=1, text="hello"):
        return Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(
                id=chat_id,
                type=ChatType.SUPERGROUP if chat_id < 0 else ChatType.PRIVATE,
            ),
            from_user=User(id=42, is_bot=False, first_name="Ali"),
            text=text,
        )

    def test_token_parser_merges_deduplicates_and_preserves_order(self):
        assert parse_bot_tokens("first, second\nfirst", "legacy") == (
            "first",
            "second",
            "legacy",
        )

    def test_same_group_message_is_processed_once_across_bots(self):
        middleware = MultiBotGroupDeduplicationMiddleware()
        handler = AsyncMock(return_value="handled")
        message = self.message()
        update = SimpleNamespace(edited_message=None)
        bot = AsyncMock()
        bot.me.return_value = SimpleNamespace(username="first_bot")

        first = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": bot})
        second = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": bot})

        assert first == "handled"
        assert second is None
        handler.assert_awaited_once()

    def test_private_messages_are_not_deduplicated(self):
        middleware = MultiBotGroupDeduplicationMiddleware()
        handler = AsyncMock(return_value="handled")
        message = self.message(chat_id=42)
        update = SimpleNamespace(edited_message=None)
        bot = AsyncMock()
        bot.me.return_value = SimpleNamespace(username="first_bot")

        async_to_sync(middleware)(handler, message, {"event_update": update, "bot": bot})
        async_to_sync(middleware)(handler, message, {"event_update": update, "bot": bot})

        assert handler.await_count == 2

    def test_targeted_message_is_processed_only_by_its_bot(self):
        middleware = MultiBotGroupDeduplicationMiddleware(("first_bot", "second_bot"))
        handler = AsyncMock(return_value="handled")
        message = self.message(text="@second_bot hello")
        update = SimpleNamespace(edited_message=None)
        first_bot = AsyncMock()
        first_bot.me.return_value = SimpleNamespace(username="first_bot")
        second_bot = AsyncMock()
        second_bot.me.return_value = SimpleNamespace(username="second_bot")

        skipped = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": first_bot})
        handled = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": second_bot})

        assert skipped is None
        assert handled == "handled"
        handler.assert_awaited_once()

    def test_slash_command_targeting_a_long_bot_username_is_recognized(self):
        middleware = MultiBotGroupDeduplicationMiddleware(("first_bot", "second_bot"))
        handler = AsyncMock(return_value="handled")
        message = self.message(text="/delete@second_bot 100")
        update = SimpleNamespace(edited_message=None)
        first_bot = AsyncMock()
        first_bot.me.return_value = SimpleNamespace(username="first_bot")
        second_bot = AsyncMock()
        second_bot.me.return_value = SimpleNamespace(username="second_bot")

        skipped = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": first_bot})
        handled = async_to_sync(middleware)(handler, message, {"event_update": update, "bot": second_bot})

        assert skipped is None
        assert handled == "handled"
        handler.assert_awaited_once()

    def test_command_targeting_unknown_other_bot_is_ignored(self):
        """A single-bot deployment must ignore /start@other_bot too."""
        middleware = MultiBotGroupDeduplicationMiddleware()
        handler = AsyncMock(return_value="handled")
        message = self.message(text="/start@mira")
        bot = AsyncMock()
        bot.me.return_value = SimpleNamespace(username="noya_bot", id=7)

        result = async_to_sync(middleware)(
            handler,
            message,
            {"event_update": SimpleNamespace(edited_message=None), "bot": bot},
        )

        assert result is None
        handler.assert_not_awaited()

    def test_emoji_router_precedes_main_router(self):
        # The main router ends with an unfiltered catch-all that would otherwise
        # consume /add_emoji and /set_welcome_emoji, so emoji_router must be
        # dispatched before the main router.
        from botapp.emoji_handlers import router as emoji_router
        from botapp.management.commands.runbot import build_dispatcher, router as main_router

        dispatcher = build_dispatcher()
        names = dispatcher.sub_routers
        self.assertIn(emoji_router, names)
        self.assertIn(main_router, names)
        self.assertLess(
            names.index(emoji_router),
            names.index(main_router),
            "emoji_router must be included before the main router",
        )

    def test_normal_user_mention_is_not_dropped(self):
        middleware = MultiBotGroupDeduplicationMiddleware(("first_bot", "second_bot"))
        handler = AsyncMock(return_value="handled")
        message = self.message(text="@some_person hello")
        bot = AsyncMock()
        bot.me.return_value = SimpleNamespace(username="first_bot")

        result = async_to_sync(middleware)(
            handler,
            message,
            {"event_update": SimpleNamespace(edited_message=None), "bot": bot},
        )

        assert result == "handled"
