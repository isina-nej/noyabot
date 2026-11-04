import asyncio
import time

from aiogram import Bot
from django.core.management.base import BaseCommand, CommandError

from botapp.telegram_config import BOT_TOKENS
from botapp.models import ModerationAction, ModerationLog
from botapp.moderation import (
    claim_due_actions,
    complete_action,
    enqueue_daily_group_schedules,
    mark_action_failed,
    requeue_stale_processing_actions,
)
from botapp.telegram_gateway import execute_telegram_action


async def execute_claimed(bots, action_id):
    action = await ModerationAction.objects.select_related("group").aget(pk=action_id)
    if action.status != "processing":
        return
    last_error = None
    for bot in bots:
        try:
            if action.target_user_id and action.action in {"mute", "ban"}:
                member = await bot.get_chat_member(action.group.chat_id, action.target_user_id)
                if member.status in {"creator", "administrator"}:
                    raise ValueError("target is a group administrator")
            await execute_telegram_action(bot, action)
        except ValueError as exc:
            await asyncio.to_thread(mark_action_failed, action, exc)
            return
        except Exception as exc:
            last_error = exc
            continue
        break
    else:
        await asyncio.to_thread(mark_action_failed, action, last_error or RuntimeError("No bot available"))
        return
    action, _ = await asyncio.to_thread(complete_action, action)
    await ModerationLog.objects.acreate(
        group=action.group,
        moderation_action=action,
        target_user_id=action.target_user_id,
        target_name=action.target_name,
        actor_user_id=action.actor_user_id,
        actor_name=action.actor_name,
        action=action.action,
        reason=action.reason,
        duration_minutes=action.duration_minutes,
    )


def _groups_for_action_ids(action_ids):
    return dict(ModerationAction.objects.filter(
        pk__in=action_ids,
    ).values_list("pk", "group_id"))


async def run_batch(limit, concurrency=1):
    await asyncio.to_thread(enqueue_daily_group_schedules)
    await asyncio.to_thread(requeue_stale_processing_actions)
    action_ids = await asyncio.to_thread(claim_due_actions, limit)
    if not action_ids:
        return 0
    bots = [Bot(token) for token in BOT_TOKENS]
    try:
        parallel = max(1, min(int(concurrency), 8))
        if parallel == 1:
            # Preserve the established order and side-effect behavior.
            for action_id in action_ids:
                await execute_claimed(bots, action_id)
        else:
            from asgiref.sync import sync_to_async

            groups = await sync_to_async(
                _groups_for_action_ids, thread_sensitive=True,
            )(action_ids)
            group_locks = {}
            slots = asyncio.Semaphore(parallel)

            async def _one(action_id):
                # Never overlap actions in the same group within a batch:
                # lock/unlock or mute/unmute order matters.
                lock = group_locks.setdefault(groups.get(action_id), asyncio.Lock())
                async with lock:
                    async with slots:
                        await execute_claimed(bots, action_id)

            await asyncio.gather(*(_one(action_id) for action_id in action_ids))
    finally:
        await asyncio.gather(*(bot.session.close() for bot in bots))
    return len(action_ids)


class Command(BaseCommand):
    help = "Execute scheduled moderation actions and automatic releases."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=15)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--concurrency", type=int, default=1,
                            help="Bounded per-batch parallelism; same-group actions remain serialized.")

    def handle(self, *args, **options):
        if not BOT_TOKENS:
            raise CommandError("BOT_TOKENS or BOT_TOKEN is not set.")
        while True:
            processed = asyncio.run(run_batch(
                max(options["limit"], 1), max(1, options["concurrency"]),
            ))
            if options["once"]:
                self.stdout.write(f"Processed {processed} actions.")
                return
            time.sleep(max(options["interval"], 1))
