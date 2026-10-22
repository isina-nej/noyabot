import asyncio
import json
import os
import unittest

from django.test import SimpleTestCase

from botapp.observability import context, metrics
from botapp.observability.logging import JsonStdlibFormatter, RING, emit
from botapp.observability.redaction import redact_value


class ObservabilityTests(SimpleTestCase):
    def tearDown(self):
        context.reset()
        metrics.reset_for_tests()

    def test_context_is_pseudonymous_and_resets(self):
        context.bind(correlation_id="abc", trace_id=context.new_trace_id(), chat_id=123, user_id=456)
        snapshot = context.snapshot()
        self.assertEqual(snapshot["correlation_id"], "abc")
        self.assertNotIn("123", snapshot["conversation_scope"])
        context.reset()
        self.assertIsNone(context.snapshot()["correlation_id"])

    def test_async_context_isolated(self):
        async def worker(value):
            context.bind(correlation_id=value)
            await asyncio.sleep(0)
            return context.snapshot()["correlation_id"]

        async def run():
            return await asyncio.gather(worker("a"), worker("b"))

        self.assertEqual(asyncio.run(run()), ["a", "b"])

    def test_redacts_credentials_and_logs_json(self):
        cleaned = redact_value({"bot_token": "123:abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "safe": "ok"})
        self.assertEqual(cleaned["bot_token"], "[REDACTED]")
        before = RING.tail(1)
        emit("test.event", prompt="private", status="ok")
        entry = RING.tail(1)[0]
        self.assertEqual(entry["event"], "test.event")
        self.assertEqual(entry["prompt"], "[REDACTED]")
        encoded = JsonStdlibFormatter().format(__import__("logging").LogRecord("x", 20, "", 0, json.dumps(entry), (), None))
        self.assertIn('"event": "test.event"', encoded)

    def test_metric_cardinality_is_bounded(self):
        for i in range(100):
            metrics.inc("noya.test.cardinality", label=str(i))
        snapshot = metrics.snapshot()
        self.assertLessEqual(len(snapshot["counters"]), 48)
