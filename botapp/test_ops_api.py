import json

from django.test import TestCase

from botapp.moderation import create_api_key
from botapp.observability import logging as obs_logging
from botapp.ops_api import _AUTH_RATE, _RATE


class OpsApiTests(TestCase):
    def setUp(self):
        _, self.raw = create_api_key("ops-tests")

    def tearDown(self):
        _AUTH_RATE.clear()
        _RATE.clear()
        super().tearDown()

    def get(self, path, **kwargs):
        return self.client.get(path, HTTP_X_API_KEY=self.raw, **kwargs)

    def test_ops_requires_auth_and_lists_safe_tests(self):
        assert self.client.get("/api/internal/ops/v1/tests/").status_code == 401
        response = self.get("/api/internal/ops/v1/tests/")
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["tests"]} == {"database", "process"}

    def test_snapshots_are_bounded_and_redacted(self):
        obs_logging.emit("ops_test", secret="bot-token-value", prompt="private text")
        response = self.get("/api/internal/ops/v1/logs/?limit=1")
        assert response.status_code == 200
        entry = response.json()["entries"][0]
        assert entry["secret"] == "[REDACTED]"
        assert entry["prompt"] == "[REDACTED]"
        assert len(response.json()["entries"]) <= 1
        assert self.get("/api/internal/ops/v1/metrics/summary/").status_code == 200
        assert self.get("/api/internal/ops/v1/traces/summary/").status_code == 200

    def test_only_registered_diagnostic_runs(self):
        response = self.client.post(
            "/api/internal/ops/v1/diagnostics/",
            data=json.dumps({"test_id": "process", "command": "echo unsafe"}),
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        )
        assert response.status_code == 400
        response = self.client.post(
            "/api/internal/ops/v1/diagnostics/",
            data=json.dumps({"test_id": "shell"}),
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        )
        assert response.status_code == 400
        response = self.client.post(
            "/api/internal/ops/v1/diagnostics/",
            data=json.dumps({"test_id": "process"}),
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "ok"
        run_id = response.json()["run_id"]
        detail = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/")
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"

    def test_health_and_trace_routes_are_private(self):
        assert self.client.get("/api/internal/ops/v1/health/live/").status_code == 401
        assert self.get("/api/internal/ops/v1/health/live/").json()["status"] == "ok"
        trace = self.get("/api/internal/ops/v1/traces/unknown/")
        assert trace.status_code == 200
        assert trace.json()["entries"] == []

    def test_diagnostic_lifecycle_aliases_expose_bounded_events_and_report(self):
        listed = self.get("/api/internal/ops/v1/diagnostics/tests/")
        assert listed.status_code == 200
        response = self.client.post(
            "/api/internal/ops/v1/diagnostics/runs/",
            data=json.dumps({"test_id": "process"}),
            content_type="application/json",
            HTTP_X_API_KEY=self.raw,
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        events = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/events/")
        report = self.get(f"/api/internal/ops/v1/diagnostics/runs/{run_id}/report/")
        assert events.status_code == report.status_code == 200
        assert events.json()["events"][-1]["event"] == "diagnostic.completed"
        assert report.json()["outcome"] == "ok"

    def test_scoped_api_key_cannot_read_other_chat_logs(self):
        key, raw = create_api_key("ops-scoped")
        key.allowed_chat_ids = [123]
        key.save(update_fields=["allowed_chat_ids"])
        obs_logging.emit("allowed_log", chat_id=123)
        obs_logging.emit("hidden_log", chat_id=456)
        response = self.client.get(
            "/api/internal/ops/v1/logs/?limit=200",
            HTTP_X_API_KEY=raw,
        )
        assert response.status_code == 200
        events = {entry.get("event") for entry in response.json()["entries"]}
        assert "allowed_log" in events
        assert "hidden_log" not in events
        assert self.client.get(
            "/api/internal/ops/v1/logs/?chat_id=456",
            HTTP_X_API_KEY=raw,
        ).status_code == 403

    def test_failed_authentication_is_rate_limited(self):
        _AUTH_RATE.clear()
        statuses = [self.client.get("/api/internal/ops/v1/tests/").status_code for _ in range(11)]
        assert statuses[-1] == 429
