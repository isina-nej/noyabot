"""Concurrency/privacy regressions for agent media outputs and moderation cancellation."""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from botapp.agent.tools import (
    _generate_image, _text_to_speech, isolated_media_results, pop_media_result,
)
from botapp.models import GroupSettings, ModerationAction
from botapp.moderation import cancel_action, complete_action, create_action


class MediaOwnershipTests(SimpleTestCase):
    def test_concurrent_image_and_voice_results_never_cross_users(self):
        @isolated_media_results
        async def turn(identity):
            # Actual handlers, with fake upstream output; no network.
            await _generate_image(prompt=identity)
            await _text_to_speech(text=identity)
            # The second turn runs before both read their results.
            await asyncio.sleep(0.02 if identity == "A" else 0)
            image = pop_media_result("image")
            voice = pop_media_result("tts")
            # Consuming a result can never yield data again.
            assert pop_media_result("image") is None
            assert pop_media_result("tts") is None
            return base64.b64decode(image), voice

        async def fake_image(prompt):
            await asyncio.sleep(0.002)
            return ("image-" + prompt).encode()

        async def fake_voice(text):
            await asyncio.sleep(0.001)
            return ("voice-" + text).encode()

        async def scenario():
            with (
                patch("botapp.services.generate_noya_image", side_effect=fake_image),
                patch("botapp.services.generate_noya_tts", side_effect=fake_voice),
            ):
                return await asyncio.gather(turn("A"), turn("B"))

        first, second = async_to_sync(scenario)()
        self.assertEqual(first, (b"image-A", b"voice-A"))
        self.assertEqual(second, (b"image-B", b"voice-B"))
        self.assertIsNone(pop_media_result("image"))


class AtomicCancellationTests(TestCase):
    def setUp(self):
        self.group = GroupSettings.objects.create(chat_id=-9090001, chat_title="test")

    def test_processing_cannot_be_cancelled_even_using_stale_action_object(self):
        action, _ = create_action(group=self.group, action="lock")
        # The worker claims it after the API selected its stale object.
        ModerationAction.objects.filter(pk=action.pk).update(status="processing")
        with self.assertRaisesRegex(ValueError, "only pending"):
            cancel_action(action)
        action.refresh_from_db()
        self.assertEqual(action.status, "processing")

    def test_cancelled_action_cannot_be_marked_executed(self):
        action, _ = create_action(group=self.group, action="lock")
        cancel_action(action)
        with self.assertRaisesRegex(ValueError, "no longer executable"):
            complete_action(action)
        action.refresh_from_db()
        self.assertEqual(action.status, "cancelled")

    def test_pending_action_can_be_cancelled_exactly_once(self):
        action, _ = create_action(group=self.group, action="lock")
        cancel_action(action)
        action.refresh_from_db()
        self.assertEqual(action.status, "cancelled")
        with self.assertRaises(ValueError):
            cancel_action(action)
