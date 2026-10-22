"""Privileged operations API contract and cross-process diagnostics regressions."""
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from botapp.moderation import create_api_key
from botapp.observability import diagnostics, store
from botapp.observability.logging import emit

READ = "read-" + "R" * 40
RUN = "run-" + "W" * 40


class OpsApiTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "NOYA_OPS_READ_TOKEN": READ,
            "NOYA_OPS_RUN_TOKEN": RUN,
            "NOYA_OPS_STATE_DIR": self.tmp.name,
            "NOYA_OPS_PERSIST_LOGS": "true",
            "NOYA_ENVIRONMENT": "production",
            "NOYA_OPS_ALLOW_STAGING_TESTS": "false",
        })
        self.env.start()
        _, self.moderation_token = create_api_key("legacy-moderation")
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def get(self, path, token=READ):
        return self.client.get(path, HTTP_X_NOYA_OPS_KEY=token)

    def post(self, path, payload, token=RUN):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json",
            HTTP_X_NOYA_OPS_KEY=token,
        )

    def test_legacy_moderation_key_cannot_read_ops_api(self):
        assert self.client.get("/api/internal/ops/v1/tests/").status_code == 401
        assert self.client.get(
            "/api/internal/ops/v1/tests/", HTTP_X_API_KEY=self.moderation_token
        ).status_code == 401

    def test_read_only_token_cannot_run_remote_diagnostics(self):
        response = self.post(
            "/api/internal/ops/v1/diagnostics/runs/", {"test_id": "process"}, token=READ
        )
        assert response.status_code == 403
        assert response.json()["error"] == "run permission required"

    def test_status_and_registered_tests_are_private(self):
        assert self.get("/api/internal/ops/v1/health/live/").status_code == 200
        tests = self.get("/api/internal/ops/v1/diagnostics/tests/").json()["tests"]
        assert "process" in {t["id"] for t in tests}
        assert all(not t["enabled"] for t in tests if t["group"] == "staging")
        assert self.get("/api/internal/ops/v1/runtime/").status_code == 200

    def test_rejects_commands_and_staging_in_production(self):
        forbidden = self.post(
            "/api/internal/ops/v1/diagnostics/runs/", {"test_id": "shell"}
        )
        assert forbidden.status_code == 400
        disabled = self.post(
            "/api/internal/ops/v1/diagnostics/runs/",
            {"test_id": "regression_security"},
        )
        assert disabled.status_code == 403
        extraneous = self.post(
            "/api/internal/ops/v1/diagnostics/runs/",
            {"test_id": "process", "command": "echo unsafe"},
        )
        assert extraneous.status_code == 400

    def test_queue_is_durable_and_worker_completes(self):
        created = self.post(
            "/api/internal/ops/v1/diagnostics/runs/", {"test_id": "process"}
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert len(run_id) == 32
        assert store.get_run(run_id)["status"] == "queued"
        with patch("botapp.observability.diagnostics.execute_one",
                   return_value=("ok", {"ok": True, "uptime_seconds": 1}, "")):
            assert diagnostics.execute_next()
        result = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/")
        assert result.status_code == 200
        assert result.json()["status"] == "completed"
        assert result.json()["outcome"] == "ok"
        events = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/events/")
        assert events.status_code == 200
        report = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/report/")
        assert report.json()["result"]["ok"]

    def test_cross_process_log_storage_redacts_prompt_and_paginates(self):
        emit("ops_test", secret="not-public", prompt="private message", status="ok")
        entries = self.get("/api/internal/ops/v1/logs/?limit=1").json()
        assert len(entries["entries"]) <= 1
        first = entries["entries"][-1]
        assert first["secret"] == "[REDACTED]"
        assert "private message" not in json.dumps(first)
        assert entries["storage"] == "shared_bounded_sqlite"
        after = self.get(f"/api/internal/ops/v1/logs/?cursor={entries['next_cursor']}")
        assert after.status_code == 200
        assert after.json()["entries"] == []

    def test_trace_id_must_be_real_hex(self):
        assert self.get("/api/internal/ops/v1/traces/../../").status_code in (404, 400)
        assert self.get("/api/internal/ops/v1/traces/invalid/").status_code == 400
        assert self.get("/api/internal/ops/v1/traces/" + "a" * 32 + "/").status_code == 200
