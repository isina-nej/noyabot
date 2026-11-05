"""Regression tests for bounded Noya provider requests.

No live Telegram or AI credentials: the provider transport is replaced in-process.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import (
    _bounded_float_env,
    _bounded_int_env,
    run_agent_loop,
)


class _Response:
    status_code = 200

    def __init__(self, message, *, finish="stop", usage=None):
        self.body = {
            "choices": [{"message": message, "finish_reason": finish}],
            "model": "test-model",
        }
        if usage is not None:
            self.body["usage"] = usage

    @property
    def text(self):
        return json.dumps(self.body)

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, responses):
        self.post = AsyncMock(side_effect=responses)
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, *_):
        self.exit_count += 1


def _ask():
    return async_to_sync(run_agent_loop)("سلام", "agent-latency-test", system_prompt="آزمایش")


class AgentLatencyBudgetTests(SimpleTestCase):
    def test_default_timeout_and_completion_budget(self):
        client = _Client([_Response({"role": "assistant", "content": "سلام!"})])
        with patch.dict(os.environ, {
            "NOYA_AGENT_TIMEOUT_S": "90",
            "NOYA_CHAT_MAX_TOKENS": "2048",
        }), patch("botapp.agent.loop.httpx.AsyncClient", return_value=client) as factory:
            reply, _ = _ask()

        self.assertEqual(reply, "سلام!")
        self.assertEqual(factory.call_args.kwargs["timeout"], 90)
        self.assertEqual(client.post.await_args.kwargs["json"]["max_tokens"], 2048)
        self.assertEqual(client.enter_count, 1)
        self.assertEqual(client.exit_count, 1)

    def test_overrides_use_bounded_values(self):
        client = _Client([_Response({"role": "assistant", "content": "جواب"})])
        with patch.dict(os.environ, {
            "NOYA_AGENT_TIMEOUT_S": "18.5",
            "NOYA_CHAT_MAX_TOKENS": "512",
        }), patch("botapp.agent.loop.httpx.AsyncClient", return_value=client) as factory:
            _ask()
        self.assertEqual(factory.call_args.kwargs["timeout"], 18.5)
        self.assertEqual(client.post.await_args.kwargs["json"]["max_tokens"], 512)

    def test_invalid_and_unbounded_settings_are_safe(self):
        with patch.dict(os.environ, {"TEST_TIMEOUT": "nan", "TEST_TOKENS": "oops"}):
            self.assertEqual(
                _bounded_float_env("TEST_TIMEOUT", 90.0, minimum=10, maximum=300),
                90.0,
            )
            self.assertEqual(
                _bounded_int_env("TEST_TOKENS", 2048, minimum=128, maximum=8192),
                2048,
            )
        with patch.dict(os.environ, {"TEST_TIMEOUT": "10000", "TEST_TOKENS": "9"}):
            self.assertEqual(
                _bounded_float_env("TEST_TIMEOUT", 90.0, minimum=10, maximum=300),
                300,
            )
            self.assertEqual(
                _bounded_int_env("TEST_TOKENS", 2048, minimum=128, maximum=8192),
                128,
            )

    def test_client_reused_across_tool_iterations(self):
        call = {
            "id": "call_1", "type": "function",
            "function": {"name": "get_time", "arguments": "{}"},
        }
        client = _Client([
            _Response({"role": "assistant", "content": "", "tool_calls": [call]}),
            _Response({"role": "assistant", "content": "الان"}),
        ])
        with patch("botapp.agent.loop.httpx.AsyncClient", return_value=client) as factory, patch(
            "botapp.agent.registry.registry.dispatch_async",
            new=AsyncMock(return_value="۱۲:۰۰"),
        ):
            reply, metadata = _ask()
        self.assertEqual(reply, "الان")
        self.assertEqual(metadata["tools_used"], ["get_time"])
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(client.enter_count, 1)
        self.assertEqual(client.exit_count, 1)

    def test_provider_usage_logged_and_metric_recorded_without_body(self):
        client = _Client([_Response(
            {"role": "assistant", "content": "پاسخ خصوصی"},
            usage={
                "prompt_tokens": 80,
                "completion_tokens": 240,
                "completion_tokens_details": {"reasoning_tokens": 120},
            },
        )])
        with patch("botapp.agent.loop.httpx.AsyncClient", return_value=client), patch(
            "botapp.agent.loop.ai_provider_request_end",
        ) as metric, self.assertLogs("botapp.agent.loop", level="INFO") as logs:
            reply, _ = _ask()
        self.assertEqual(reply, "پاسخ خصوصی")
        metric.assert_called_once()
        self.assertEqual(metric.call_args.kwargs["tokens_in"], 80)
        self.assertEqual(metric.call_args.kwargs["tokens_out"], 240)
        provider_logs = [line for line in logs.output if "[NOYA-PROVIDER]" in line]
        self.assertEqual(len(provider_logs), 1)
        self.assertIn("reasoning_tokens=120", provider_logs[0])
        self.assertNotIn("پاسخ خصوصی", provider_logs[0])

    def test_truncation_never_dispatches_incomplete_tool_calls(self):
        call = {
            "id": "call_1", "type": "function",
            "function": {"name": "get_time", "arguments": "{"},
        }
        client = _Client([_Response(
            {"role": "assistant", "content": "", "tool_calls": [call]},
            finish="length",
        )])
        with patch("botapp.agent.loop.httpx.AsyncClient", return_value=client), patch(
            "botapp.agent.registry.registry.dispatch_async", new=AsyncMock()
        ) as dispatch:
            reply, metadata = _ask()
        self.assertTrue(metadata["response_truncated"])
        self.assertIn("سقف طول", reply)
        dispatch.assert_not_awaited()

    def test_timeout_returns_clean_message_and_closes_client(self):
        client = _Client([httpx.ReadTimeout("slow")])
        with patch.dict(os.environ, {"NOYA_AGENT_TIMEOUT_S": "20"}), patch(
            "botapp.agent.loop.httpx.AsyncClient", return_value=client
        ):
            reply, metadata = _ask()
        self.assertIn("تایم‌اوت", reply)
        self.assertEqual(metadata, {})
        self.assertEqual(client.exit_count, 1)

    def test_empty_choices_are_rejected_without_crashing(self):
        class EmptyResponse(_Response):
            def __init__(self):
                self.body = {"choices": []}
        client = _Client([EmptyResponse()])
        with patch("botapp.agent.loop.httpx.AsyncClient", return_value=client):
            reply, metadata = _ask()
        self.assertIn("خطا در پردازش پاسخ", reply)
        self.assertEqual(metadata, {})

    def test_optional_reasoning_effort_added_when_configured(self):
        client = _Client([_Response({"role": "assistant", "content": "سلام!"})])
        with patch.dict(os.environ, {"NOYA_AGENT_REASONING_EFFORT": "low"}), patch(
            "botapp.agent.loop.httpx.AsyncClient", return_value=client
        ):
            reply, _ = _ask()
        self.assertEqual(reply, "سلام!")
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_tokens"], 2048)

    def test_unsupported_reasoning_effort_is_not_sent(self):
        for value in ("", "unsupported", "DROP TABLE users"):
            client = _Client([_Response({"role": "assistant", "content": "سلام!"})])
            with patch.dict(os.environ, {"NOYA_AGENT_REASONING_EFFORT": value}), patch(
                "botapp.agent.loop.httpx.AsyncClient", return_value=client
            ):
                reply, _ = _ask()
            self.assertEqual(reply, "سلام!")
            self.assertNotIn("reasoning_effort", client.post.await_args.kwargs["json"])
