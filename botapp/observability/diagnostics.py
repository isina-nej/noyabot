"""Allowlisted diagnostics executed only in a separate worker subprocess.

The HTTP API enqueues work; no user-controlled command, module, path, URL or
Python source is ever supplied to subprocess execution.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from . import store

# ID -> fixed test definition. These are the ONLY runnable modules.
PROBES = {
    "process": {"group": "production", "description": "Local process and runtime status"},
    "database": {"group": "production", "description": "Read-only SELECT 1 on the configured database"},
    "deployment": {"group": "production", "description": "Git revision and runtime version"},
    "telegram_heartbeat": {"group": "production", "description": "Bot worker heartbeat from shared ops state"},
    "schema": {"group": "production", "description": "Read-only Django migrations table availability"},
    "telemetry": {"group": "production", "description": "OpenTelemetry configured state"},
    "provider_tcp": {"group": "production", "description": "Configured AI endpoint TCP only; no model call"},
    "regression_outputs": {"group": "staging", "description": "Isolated AI response and output-sanitizer regressions"},
    "regression_security": {"group": "staging", "description": "Isolated Harness security tests"},
    "regression_memory": {"group": "staging", "description": "Isolated memory/relationship tests"},
}

STAGING_MODULES = {
    "regression_outputs": ["botapp.test_agent_output_paths", "botapp.test_response_cleaning"],
    "regression_security": ["botapp.test_harness_security"],
    "regression_memory": ["botapp.test_memory_integration", "botapp.test_relationship_runtime"],
}


def is_staging_enabled() -> bool:
    env = os.getenv("NOYA_ENVIRONMENT", "development").lower()
    return env in ("staging", "development", "test") and os.getenv(
        "NOYA_OPS_ALLOW_STAGING_TESTS", "false"
    ).lower() in ("1", "true", "yes", "on")


def test_catalog():
    return [
        {"id": name, **entry, "enabled": entry["group"] == "production" or is_staging_enabled()}
        for name, entry in PROBES.items()
    ]


def can_enqueue(test_id: str):
    item = PROBES.get(test_id)
    if not item:
        return False, "unknown test_id"
    if item["group"] == "staging" and not is_staging_enabled():
        return False, "staging diagnostics disabled"
    return True, ""


def probe(test_id: str) -> dict:
    """Called only from this module's fixed worker CLI mode."""
    from . import health, tracing
    if test_id == "process":
        return {"ok": True, "uptime_seconds": round(health.process_uptime_seconds(), 2)}
    if test_id == "database":
        ok, error = health.db_ready()
        return {"ok": ok, "error_type": error or None}
    if test_id == "deployment":
        from .logging import resolve_git_commit
        return {"ok": True, "git_commit": resolve_git_commit(), "release": os.getenv("NOYA_RELEASE", "")[:80]}
    if test_id == "telegram_heartbeat":
        return {"ok": bool(store.read_heartbeat().get("healthy")), "heartbeat": store.read_heartbeat()}
    if test_id == "schema":
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                # Pure read; avoids django's migrate --check touching the prod DB.
                cursor.execute("SELECT COUNT(*) FROM django_migrations")
                count = int(cursor.fetchone()[0])
            return {"ok": count > 0, "applied_migrations": count}
        except Exception as exc:
            return {"ok": False, "error_type": type(exc).__name__}
    if test_id == "telemetry":
        return {"ok": True, "provider": tracing.trace_provider(), "enabled": tracing.enabled()}
    if test_id == "provider_tcp":
        from urllib.parse import urlsplit
        target = urlsplit(os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions"))
        if target.scheme not in ("http", "https") or not target.hostname:
            return {"ok": False, "error_type": "InvalidConfiguration"}
        ok, error = health.tcp_reachable(target.hostname, target.port or (443 if target.scheme == "https" else 80), timeout=1.5)
        return {"ok": ok, "error_type": error or None}
    raise ValueError("unknown registered test")


def _restricted_child():
    if os.name == "posix":
        try:
            import resource
            # Test startup needs memory for Django imports; CPU/virtual address
            # ceilings bound runaway tests without shell or elevated privileges.
            resource.setrlimit(resource.RLIMIT_CPU, (20, 22))
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        except (ImportError, OSError, ValueError):
            pass


def execute_one(test_id: str) -> tuple[str, dict, str]:
    allowed, why = can_enqueue(test_id)
    if not allowed:
        return "failed", {"ok": False, "error_type": "DisabledOrUnknownTest"}, "DisabledOrUnknownTest"
    staging = test_id in STAGING_MODULES
    timeout = 45 if staging else 10
    if staging:
        cmd = [sys.executable, "manage.py", "test", *STAGING_MODULES[test_id], "--noinput"]
        # A disposable SQLite DB and no real messaging/provider credentials.
        env = dict(os.environ)
        env.update({
            "DB_ENGINE": "django.db.backends.sqlite3",
            "DB_NAME": ":memory:",
            "BOT_TOKEN": "", "BOT_TOKENS": "", "NOYA_API_KEY": "",
            "AI_API_KEY": "", "TELETHON_SESSION_PATH": "",
            "MESSAGE_ARCHIVE_ENABLED": "false",
        })
    else:
        cmd = [sys.executable, "-m", "botapp.observability.diagnostics", "--probe", test_id]
        env = os.environ.copy()
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd, cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env=env, capture_output=True, timeout=timeout, check=False,
            preexec_fn=_restricted_child if os.name == "posix" else None,
        )
        duration = round((time.monotonic() - started) * 1000)
        if staging:
            # Never persist raw stdout/stderr containing possible fixture values.
            ok = result.returncode == 0
            return ("ok" if ok else "failed",
                    {"ok": ok, "exit_code": result.returncode, "duration_ms": duration,
                     "test_group": test_id}, "" if ok else "TestFailure")
        raw = result.stdout[:4096]
        payload = json.loads(raw.decode("utf-8")) if result.returncode == 0 else {"ok": False, "error_type": "ProbeFailed"}
        ok = bool(payload.get("ok"))
        return ("ok" if ok else "failed",
                {**payload, "duration_ms": duration},
                "" if ok else str(payload.get("error_type") or "ProbeFailed")[:80])
    except subprocess.TimeoutExpired:
        return "failed", {"ok": False, "error_type": "Timeout", "timeout_seconds": timeout}, "Timeout"
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return "failed", {"ok": False, "error_type": type(exc).__name__}, type(exc).__name__


def execute_next():
    selected = store.claim_run()
    if not selected:
        return False
    test_id, run_id = selected["test_id"], selected["run_id"]
    try:
        outcome, result, error_type = execute_one(test_id)
    except Exception as exc:
        outcome, result, error_type = "failed", {"ok": False}, type(exc).__name__
    store.complete_run(run_id, outcome=outcome, result=result, error_type=error_type)
    return True


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2 or args[0] != "--probe" or args[1] not in PROBES or args[1] in STAGING_MODULES:
        raise SystemExit(2)
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    try:
        result = probe(args[1])
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__}
    print(json.dumps(result, default=str, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    main()
