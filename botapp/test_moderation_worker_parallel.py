"""Concurrency regression for the optional moderation worker parallel mode."""
import asyncio
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.management.commands.run_moderation_worker import run_batch


class ParallelModerationWorkerTests(SimpleTestCase):
    def test_parallel_groups_remain_ordered_within_each_group(self):
        groups = {1: 10, 2: 10, 3: 20}
        running_groups = set()
        active = 0
        peak = 0
        same_group_overlap = False
        executed = []

        async def fake_execute(bots, action_id):
            nonlocal active, peak, same_group_overlap
            group = groups[action_id]
            if group in running_groups:
                same_group_overlap = True
            running_groups.add(group)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            executed.append(action_id)
            active -= 1
            running_groups.remove(group)

        class DummyBot:
            def __init__(self, token):
                self.session = type("Session", (), {"close": AsyncMock()})()

        with (
            patch("botapp.management.commands.run_moderation_worker.BOT_TOKENS", ("token",)),
            patch("botapp.management.commands.run_moderation_worker.Bot", DummyBot),
            patch("botapp.management.commands.run_moderation_worker.enqueue_daily_group_schedules"),
            patch("botapp.management.commands.run_moderation_worker.requeue_stale_processing_actions"),
            patch("botapp.management.commands.run_moderation_worker.claim_due_actions",
                  return_value=[1, 2, 3]),
            patch("botapp.management.commands.run_moderation_worker._groups_for_action_ids",
                  return_value=groups),
            patch("botapp.management.commands.run_moderation_worker.execute_claimed",
                  new=fake_execute),
        ):
            count = async_to_sync(run_batch)(3, 2)

        self.assertEqual(count, 3)
        self.assertFalse(same_group_overlap)
        self.assertEqual(sorted(executed), [1, 2, 3])
        self.assertLess(executed.index(1), executed.index(2))
        self.assertEqual(peak, 2)
