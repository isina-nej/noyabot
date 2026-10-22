from django.contrib import admin
from django.urls import path

from botapp.api import (
    action_detail_api,
    cancel_action_api,
    create_action_api,
    group_settings_api,
)
from botapp.views import healthcheck
from botapp.ops_api import (
    ops_dependencies, ops_diagnostic_run_detail, ops_diagnostics, ops_health, ops_live, ops_logs, ops_metrics, ops_ready, ops_runtime, ops_tests, ops_traces, ops_trace_detail,
)

urlpatterns = [
    path("health/", healthcheck, name="healthcheck"),
    path("api/internal/ops/v1/health/", ops_health, name="ops-health"),
    path("api/internal/ops/v1/health/live/", ops_live, name="ops-health-live"),
    path("api/internal/ops/v1/health/ready/", ops_ready, name="ops-health-ready"),
    path("api/internal/ops/v1/health/dependencies/", ops_dependencies, name="ops-health-dependencies"),
    path("api/internal/ops/v1/runtime/", ops_runtime, name="ops-runtime"),
    path("api/internal/ops/v1/logs/", ops_logs, name="ops-logs"),
    path("api/internal/ops/v1/metrics/summary/", ops_metrics, name="ops-metrics"),
    path("api/internal/ops/v1/traces/summary/", ops_traces, name="ops-traces"),
    path("api/internal/ops/v1/traces/<str:trace_id>/", ops_trace_detail, name="ops-trace-detail"),
    path("api/internal/ops/v1/diagnostics/", ops_diagnostics, name="ops-diagnostics"),
    path("api/internal/ops/v1/diagnostics/run/", ops_diagnostics, name="ops-diagnostics-run"),
    path("api/internal/ops/v1/diagnostics/runs/<str:run_id>/", ops_diagnostic_run_detail, name="ops-diagnostic-run-detail"),
    path("api/internal/ops/v1/tests/", ops_tests, name="ops-tests"),
    path("api/groups/<str:chat_id>/settings/", group_settings_api, name="group-settings-api"),
    path("api/moderation/actions/", create_action_api, name="create-action-api"),
    path("api/moderation/actions/<int:action_id>/", action_detail_api, name="action-detail-api"),
    path("api/moderation/actions/<int:action_id>/cancel/", cancel_action_api, name="cancel-action-api"),
    path("admin/", admin.site.urls),
]
