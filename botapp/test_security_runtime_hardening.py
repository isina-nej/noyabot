"""Regression tests for hardened admin targets, chat JSON schemas and sandbox."""
import json
import logging
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from pydantic import BaseModel

from botapp.agent.context import AgentContext
from botapp.agent.errors import ProtectedTarget
from botapp.agent.orchestrator import _ensure_target_allowed
from botapp.agent.permissions import AdminIdentity, BotCapabilities
from botapp.agent.registry import CHAT, Tool, ToolRegistry
from botapp.agent.sandbox import run_sandboxed
from botapp.observability.logging import JsonStdlibFormatter


class ProtectedTargetFailClosedTests(SimpleTestCase):
    def _context(self):
        return AgentContext(
            chat_id=-1001, chat_type="supergroup", chat_title="Example",
            admin=AdminIdentity(user_id=1, role="administrator"),
            bot_capabilities=BotCapabilities(is_admin=True),
        )

    def test_telegram_lookup_failure_is_never_permission(self):
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(id=1000)),
            get_chat_member=AsyncMock(side_effect=RuntimeError("temporarily unavailable")),
        )
        tool = SimpleNamespace(name="member.ban")
        with self.assertRaises(ProtectedTarget):
            async_to_sync(_ensure_target_allowed)(bot, self._context(), tool, 11)

    def test_admin_target_blocked(self):
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(id=1000)),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
        )
        tool = SimpleNamespace(name="member.mute")
        with self.assertRaises(ProtectedTarget):
            async_to_sync(_ensure_target_allowed)(bot, self._context(), tool, 11)

    def test_normal_member_allowed(self):
        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(id=1000)),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        )
        tool = SimpleNamespace(name="member.warn")
        self.assertIsNone(async_to_sync(_ensure_target_allowed)(bot, self._context(), tool, 11))


class ChatToolValidationTests(SimpleTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.calls = []

        async def handler(query):
            self.calls.append(query)
            return "OK"

        self.registry.register(Tool(
            name="test.lookup", description="test", category=CHAT,
            handler=handler,
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 120}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ))

    def test_valid_tool_runs(self):
        self.assertEqual(
            async_to_sync(self.registry.dispatch_async)("test.lookup", {"query": "hello"}), "OK",
        )
        self.assertEqual(self.calls, ["hello"])

    def test_invalid_type_missing_and_oversized_are_rejected_before_handler(self):
        for args in ({"query": 123}, {}, {"query": "a" * 121},
                     {"query": "ok", "secret": "unexpected"},
                     {"query": "a" * 17000}, ["not", "a", "dict"]):
            answer = async_to_sync(self.registry.dispatch_async)("test.lookup", args)
            self.assertIn("invalid input", answer)
        self.assertEqual(self.calls, [])

    def test_admin_tool_cannot_be_reached_by_chat_dispatch(self):
        self.registry.register(Tool(
            name="member.ban", description="admin only",
            category="admin", handler=lambda **kwargs: "bad",
        ))
        answer = async_to_sync(self.registry.dispatch_async)("member.ban", {})
        self.assertIn("not available", answer)


class SandboxIsolationTests(SimpleTestCase):
    @patch.dict(os.environ, {"NOYA_SANDBOX_ENABLED": "true"})
    def test_harmless_analysis_uses_subprocess(self):
        result = run_sandboxed("result = sum(x['n'] for x in data['items'])",
                               {"items": [{"n": 1}, {"n": 2}]}, timeout_seconds=3)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.result, 3)

    @patch.dict(os.environ, {"NOYA_SANDBOX_ENABLED": "true"})
    def test_infinite_loop_is_reaped_within_deadline(self):
        start = time.monotonic()
        result = run_sandboxed("while True:\n    pass", timeout_seconds=0.25)
        self.assertFalse(result.ok)
        self.assertIn(result.error, {"timeout", "sandbox_process_failed"})
        self.assertLess(time.monotonic() - start, 3)

    @patch.dict(os.environ, {"NOYA_SANDBOX_ENABLED": "false"})
    def test_production_kill_switch(self):
        self.assertEqual(run_sandboxed("result = 1").error, "sandbox_disabled")


class LogPrivacyTests(SimpleTestCase):
    def test_unstructured_log_message_is_never_published(self):
        record = logging.LogRecord("botapp", logging.INFO, "", 0, "private user prompt 123", (), None)
        result = json.loads(JsonStdlibFormatter().format(record))
        self.assertNotIn("private user prompt", json.dumps(result))
        self.assertEqual(result.get("message"), "[REDACTED]")

    def test_structured_fields_are_redacted(self):
        record = logging.LogRecord(
            "botapp", logging.INFO, "", 0,
            json.dumps({"event": "chat", "prompt": "my secret", "status": "ok"}),
            (), None,
        )
        result = json.loads(JsonStdlibFormatter().format(record))
        self.assertEqual(result["event"], "chat")
        self.assertEqual(result["prompt"], "[REDACTED]")


class XBridgeAuthorizationTests(SimpleTestCase):
    @staticmethod
    def _load_bridge():
        # The bridge is a standalone script in a hyphenated directory.
        import importlib.util
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "x-bridge" / "server.py"
        spec = importlib.util.spec_from_file_location("noya_x_bridge_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @patch.dict(os.environ, {"X_BRIDGE_INTERNAL_TOKEN": ""})
    def test_missing_token_denies_every_request(self):
        bridge = self._load_bridge()
        self.assertFalse(bridge.is_authorized({"Authorization": "Bearer any"}))

    @patch.dict(os.environ, {"X_BRIDGE_INTERNAL_TOKEN": "unit-test-secret"})
    def test_valid_bearer_is_required(self):
        bridge = self._load_bridge()
        self.assertFalse(bridge.is_authorized({}))
        self.assertFalse(bridge.is_authorized({"Authorization": "Bearer invalid"}))
        self.assertTrue(bridge.is_authorized({"Authorization": "Bearer unit-test-secret"}))


class DeploymentSeparationTests(SimpleTestCase):
    def test_worker_imports_token_config_without_loading_runbot(self):
        from botapp import telegram_config

        self.assertEqual(
            telegram_config.parse_bot_tokens("t1, t2\nt1", "t3"),
            ("t1", "t2", "t3"),
        )


class SSRFTransportTests(SimpleTestCase):
    def test_secure_backend_connects_to_validated_ip_not_untrusted_hostname(self):
        import socket
        import httpcore
        from botapp.web.security import SafeNetworkBackend

        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                    ("1.1.1.1", 443))]
        with (
            patch("botapp.web.security.socket.getaddrinfo", return_value=answers),
            patch.object(httpcore.AnyIOBackend, "connect_tcp", new_callable=AsyncMock) as tcp,
        ):
            async_to_sync(SafeNetworkBackend().connect_tcp)("example.org", 443)
        tcp.assert_awaited_once()
        self.assertEqual(tcp.await_args.args[0], "1.1.1.1")
        self.assertEqual(tcp.await_args.args[1], 443)

    def test_secure_backend_rejects_private_dns_response(self):
        import socket
        from botapp.web.errors import BlockedURLError
        from botapp.web.security import SafeNetworkBackend

        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("botapp.web.security.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(BlockedURLError):
                async_to_sync(SafeNetworkBackend().connect_tcp)("example.org", 443)


class ProductionConfigurationTests(SimpleTestCase):
    @patch.dict(os.environ, {"NOYA_ENVIRONMENT": "production", "DEBUG": "true"})
    def test_production_rejects_debug(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "NOYA_ENVIRONMENT": "production", "DEBUG": "true"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DEBUG must be false", proc.stderr)

    def test_sync_harness_execution_denied_in_production(self):
        from botapp.harness.executor import HarnessExecutor
        from botapp.harness.errors import StepTimeoutError

        with patch.dict(os.environ, {
            "NOYA_ENVIRONMENT": "production",
            "NOYA_ALLOW_SYNC_HARNESS": "false",
        }):
            with self.assertRaises(StepTimeoutError):
                HarnessExecutor._exec_sync(None, None, None)
