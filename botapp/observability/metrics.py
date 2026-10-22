"""Low-cardinality metrics registry (no external dependency).

Counters/histograms/gauges live in process memory; they power
``/api/internal/ops/v1/metrics/summary`` and are OTel-exportable later if a
collector runs alongside (beyond chat scope; never per-user labels).
All label values are bounded and scrubbed.
"""
from __future__ import annotations

import math
import os
import threading
import time

_LOCK = threading.Lock()
_METRICS: dict[str, dict] = {}


def _bucket_label(labels: dict) -> tuple:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _limit_cardinality(base: str, labels: tuple) -> bool:
    """Refuse > 48 series per base to stop label blow-ups."""
    key = (base, labels)
    if key in _METRICS:
        return True
    sameseries = sum(1 for k in _METRICS if k[0] == base)
    return sameseries < 48


def inc(name: str, amount: float = 1.0, **labels) -> None:
    key = (name, _bucket_label(labels))
    with _LOCK:
        if not _limit_cardinality(name, key[1]):
            return
        entry = _METRICS.setdefault(key, {"type": "counter", "value": 0.0})
        entry["value"] += max(0.0, float(amount))


def observe(name: str, value: float, buckets=(5, 25, 50, 100, 250, 500,
                                            1000, 2500, 5000, 10000), **labels) -> None:
    """Logarithmic-ish histogram buckets (milliseconds)."""
    key = (name, _bucket_label(labels))
    value = float(value)
    with _LOCK:
        if not _limit_cardinality(name, key[1]):
            return
        entry = _METRICS.setdefault(
            key, {"type": "histogram", "sum": 0.0, "count": 0,
                  "buckets": {b: 0 for b in buckets}}
        )
        entry["sum"] += value
        entry["count"] += 1
        for bucket in buckets:
            if value <= bucket:
                entry["buckets"][bucket] += 1


def set_gauge(name: str, value: float, **labels) -> None:
    key = (name, _bucket_label(labels))
    with _LOCK:
        if not _limit_cardinality(name, key[1]):
            return
        entry = _METRICS.setdefault(key, {"type": "gauge", "value": 0.0})
        entry["value"] = float(value)


def snapshot() -> dict:
    """Bounded, JSON-friendly dump with p-estimates by interpolation."""
    with _LOCK:
        out: dict[str, list] = {}
        for (base, labels), entry in _METRICS.items():
            label_str = ",".join(f"{k}={v}" for k, v in labels)
            key_name = f"{base}|{label_str}" if label_str else base
            if entry["type"] == "counter":
                out.setdefault("counters", []).append(
                    {"name": key_name, "labels": dict(labels), "value": entry["value"]}
                )
            elif entry["type"] == "gauge":
                out.setdefault("gauges", []).append(
                    {"name": key_name, "labels": dict(labels), "value": entry["value"]}
                )
            else:  # histogram
                total = entry["count"]
                ordered = sorted(entry["buckets"].items())
                def estimate(q: float) -> float | None:
                    if not total:
                        return None
                    target = math.ceil(q * total)
                    for bucket, count in ordered:
                        if count >= target:
                            return float(bucket)
                    return None
                out.setdefault("histograms", []).append({
                    "name": key_name,
                    "labels": dict(labels),
                    "count": total,
                    "sum": round(entry["sum"], 2),
                    "p50": estimate(0.5),
                    "p95": estimate(0.95),
                    "p99": estimate(0.99),
                })
        return out


def reset_for_tests() -> None:
    with _LOCK:
        _METRICS.clear()


# ── standard metric names (single source of truth) ────────────────────────
TELEGRAM_UPDATES_RECEIVED = "noya.telegram.updates_received"
TELEGRAM_UPDATES_PROCESSED = "noya.telegram.updates_processed"
TELEGRAM_UPDATES_IGNORED = "noya.telegram.updates_ignored"
TELEGRAM_HANDLER_FAILURES = "noya.telegram.handler_failures"
TELEGRAM_SEND_FAILURES = "noya.telegram.send_failures"
TELEGRAM_UPDATE_LATENCY = "noya.telegram.update_latency_ms"

AI_PROVIDER_REQUESTS = "noya.ai.provider_requests"
AI_PROVIDER_FAILURES = "noya.ai.provider_failures"
AI_PROVIDER_TIMEOUTS = "noya.ai.provider_timeouts"
AI_PROVIDER_MALFORMED = "noya.ai.provider_malformed"
AI_SSE_ERRORS = "noya.ai.sse_errors"
AI_LATENCY = "noya.ai.provider_latency_ms"
AI_STREAM_TIME = "noya.ai.streaming_ms"
AI_TOKENS_INPUT = "noya.ai.tokens_input_total"
AI_TOKENS_OUTPUT = "noya.ai.tokens_output_total"

AGENT_TOOL_CALLS = "noya.agent.tool_calls"
AGENT_TOOL_FAILURES = "noya.agent.tool_failures"
AGENT_TOOL_LATENCY = "noya.agent.tool_latency_ms"

HARNESS_RUNS_STARTED = "noya.harness.runs_started"
HARNESS_RUNS_FAILED = "noya.harness.runs_failed"
HARNESS_STEPS = "noya.harness.steps_completed"
HARNESS_STEP_FAILURES = "noya.harness.steps_failed"
HARNESS_PERMISSION_DENIALS = "noya.harness.permission_denials"
HARNESS_STEP_DURATION = "noya.harness.step_duration_ms"

MEMORY_READ_FAILURES = "noya.memory.read_failures"
MEMORY_WRITE_FAILURES = "noya.memory.write_failures"
MEMORY_LATENCY = "noya.memory.lookup_latency_ms"

EMOTION_CLASSIFICATIONS = "noya.emotion.event_classifications"
EMOTION_CONFLICT_TRANSITIONS = "noya.emotion.conflict_transitions"
EMOTION_DUPLICATE_EVENTS = "noya.emotion.duplicate_events_detected"
EMOTION_PERSISTENCE_FAILURES = "noya.emotion.persistence_failures"

OUTPUT_ARTIFACTS_DETECTED = "noya.output.internal_artifacts_detected"
OUTPUT_SANITIZATIONS = "noya.output.sanitizations"
OUTPUT_EMPTY_RESPONSE = "noya.output.empty_responses"

OPS_AUTH_FAILURES = "noya.ops.auth_failures"
OPS_RATE_LIMITED = "noya.ops.rate_limited"
OPS_DIAGNOSTIC_RUNS = "noya.ops.diagnostic_runs"

UPS_SECONDS = "noya.process.uptime_seconds"
PROCESS_RSS_MB = "noya.process.rss_mb"


def start_time() -> float:
    import os as _os

    return _os.stat("/proc/self").st_mtime
