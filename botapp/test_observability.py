import asyncio
import json
import os
import unittest

from django.test import SimpleTestCase

from botapp.observability import context, metrics
from botapp.observability.logging import JsonStdlibFormatter, RING, emit
from botapp.observability.redaction import redact_value
from botapp.observability import tracing


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

    def test_redacts_embedded_bearer_jwt_and_long_tokens(self):
        value = "Bearer " + "a" * 40
        jwt = "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8
        cleaned = redact_value({"error": value + " " + jwt + " " + "f" * 64})
        self.assertNotIn("Bearer", cleaned["error"])
        self.assertNotIn(jwt, cleaned["error"])
        self.assertNotIn("f" * 64, cleaned["error"])

    def test_fallback_spans_are_unique_and_restore_context(self):
        with tracing.start_span("stage"):
            first = context.span_id_var.get()
            with tracing.start_span("stage"):
                second = context.span_id_var.get()
            self.assertEqual(context.span_id_var.get(), first)
        self.assertNotEqual(first, second)
        self.assertEqual(context.span_id_var.get(), "")

    def test_request_id_is_carried_in_snapshot(self):
        context.bind(request_id="req-123")
        self.assertEqual(context.snapshot()["request_id"], "req-123")

    def test_malformed_sse_has_no_valid_chunks(self):
        from botapp.agent.loop import _parse_sse_stream
        self.assertIsNone(_parse_sse_stream("data: not-json\n\ndata: [DONE]\n"))

    def test_provider_retry_and_timeout_metrics(self):
        from botapp.observability.instrumentation import ai_provider_request_end
        ai_provider_request_end(duration_ms=12, status="timeout", model="m", timed_out=True, retry=1)
        snapshot = metrics.snapshot()
        names = {x["name"] for x in snapshot["counters"]}
        self.assertIn("noya.ai.provider_failures|kind=timeout,model=m", names)
        self.assertIn("noya.ai.provider_timeouts|model=m", names)
        self.assertIn("noya.ai.retries|model=m", names)
