"""Circular-buffer log storage + structlog/stdlib JSON formatting.

Design decisions:
* A bounded in-process deque is the Profile-A log source of truth — honest
  about limits: process-restart logs are lost (documented, not papered over).
* ``structlog`` is OPTIONAL: when not installed a stdlib JSON renderer with
  identical field schemas does the job.  Nothing here ever raises.
* One process initializes handlers once (module-level guard), so AIOSQLite
  workers and HTTP servers never stack duplicated handlers.
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import threading
import time
import subprocess

from . import context as ctx
from .redaction import redact_value

DEFAULT_CAPACITY = int(os.getenv("NOYA_LOG_RING_CAPACITY", "2000"))
DEFAULT_RETENTION_LINES = int(os.getenv("NOYA_LOG_RETENTION_LINES", "50000"))
_LEVEL_BY_NAME = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

try:  # optional dependency
    import structlog
except Exception:  # pragma: no cover - structlog not installed
    structlog = None

_SERVICE = os.getenv("NOYA_SERVICE_NAME", "noyabot")
_ENVIRONMENT = os.getenv("NOYA_ENVIRONMENT", "development")
_RELEASE = os.getenv("NOYA_RELEASE", "")
_GIT_COMMIT = os.getenv("NOYA_GIT_COMMIT", "")
_RESOLVED_COMMIT: str | None = None


def resolve_git_commit() -> str:
    """Best-effort static git commit from env or a local GIT_COMMIT marker."""
    global _RESOLVED_COMMIT
    if _RESOLVED_COMMIT is not None:
        return _RESOLVED_COMMIT
    if _GIT_COMMIT:
        _RESOLVED_COMMIT = _GIT_COMMIT[:64]
        return _RESOLVED_COMMIT
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            stderr=subprocess.DEVNULL,
            timeout=0.5,
            text=True,
        ).strip()
        _RESOLVED_COMMIT = value[:64]
        return _RESOLVED_COMMIT
    except Exception:
        _RESOLVED_COMMIT = ""
        return ""


class RingBufferStore:
    """Thread-safe bounded in-memory log store with cursor-based reads."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self.capacity = max(100, min(int(capacity), 20000))
        self._entries: collections.deque = collections.deque(maxlen=self.capacity)
        self._counter = 0
        self._lock = threading.Lock()

    def append(self, entry: dict) -> None:
        with self._lock:
            self._counter += 1
            entry = dict(entry)
            entry["_seq"] = self._counter
            self._entries.append(entry)

    def since(self, cursor: int = 0) -> tuple[list[dict], int]:
        """Entries after *cursor* (exclusive) and the newest cursor."""
        with self._lock:
            items = [e for e in self._entries if e.get("_seq", 0) > cursor]
            return items, self._counter

    def tail(self, count: int = 200) -> list[dict]:
        with self._lock:
            return list(self._entries)[-max(0, count):]

    def trim_windows(self) -> None:
        """Rotating trim is unnecessary for a deque (auto-bounded); hook kept
        so callers/tests can prove capacity enforcement."""
        with self._lock:
            while len(self._entries) > self.capacity:
                self._entries.popleft()


RING = RingBufferStore()


def _base_fields() -> dict:
    fields = {
        "timestamp": ctx.utc_now_iso(),
        "service": _SERVICE,
        "environment": _ENVIRONMENT,
        "release": _RELEASE,
        "git_commit": resolve_git_commit(),
        "level": None,
        "event": None,
    }
    fields.update({k: v for k, v in ctx.snapshot().items() if v})
    return fields


def emit(event: str, *, level: str = "info", logger_name: str = "botapp.observability",
         **fields) -> None:
    """Structured JSON emission — logging handler + bounded ring. Never raises."""
    level = (level or "info").casefold()
    payload = _base_fields()
    payload["event"] = str(event)
    payload["level"] = level
    try:
        clean = redact_value(fields)
    except Exception:
        clean = {"redaction_error": True}
    payload.update(clean)
    RING.append(payload)
    # The Django HTTP process and the Telegram polling process do not share
    # RAM; keep bounded redacted logs in their shared private ops store.
    if os.getenv('NOYA_OPS_PERSIST_LOGS', 'true').lower() in ('true', '1', 'yes', 'on'):
        try:
            from .store import record_event
            record_event(payload)
        except Exception:
            pass  # telemetry must not interrupt application work
    std_logger = logging.getLogger(logger_name or "botapp.observability")
    record = logging.LogRecord(
        name=logger_name or "botapp.observability",
        level=_LEVEL_BY_NAME.get(level, logging.INFO),
        pathname="observability",
        lineno=0,
        msg=json.dumps({k: payload.get(k) for k in (
            "timestamp", "service", "environment", "release", "git_commit",
            "event", "level", "correlation_id", "trace_id", "span_id",
            "operation", "duration_ms", "status", "error_type")},
            default=str, ensure_ascii=False),
        args=(),
        exc_info=None,
    )
    record.created = time.time()
    std_logger.handle(record)


class SafeOpsStoreHandler(logging.Handler):
    """Record safe metadata for legacy WARNING/ERROR log records.

    Never serialize record.message, args, traceback or provider response.
    Those legacy strings can embed private Telegram messages and credentials.
    """
    def emit(self, record):
        try:
            if record.name.startswith("botapp.observability"):
                return
            from .store import record_event
            entry = _base_fields()
            entry.update({
                "event": "legacy.log",
                "level": record.levelname.lower(),
                "source_logger": record.name[:100],
                "error_type": (
                    type(record.exc_info[1]).__name__
                    if record.exc_info and record.exc_info[1] else ""
                ),
            })
            record_event(entry)
        except Exception:
            pass


class JsonStdlibFormatter(logging.Formatter):
    """stdlib formatter producing the same JSON schema as :func:`emit`."""

    def format(self, record: logging.LogRecord) -> str:
        payload = _base_fields()
        payload["event"] = record.getMessage().split(" ", 1)[0][:80]
        try:
            maybe = json.loads(record.getMessage())
            if isinstance(maybe, dict):
                payload.update({k: v for k, v in maybe.items() if k != "event"})
                payload["event"] = str(maybe.get("event") or payload["event"])
        except Exception:
            # Arbitrary legacy logger messages may contain Telegram text,
            # provider payloads or secrets. Never export their raw contents.
            payload["message"] = "[UNSTRUCTURED_MESSAGE_REDACTED]"
        payload["level"] = record.levelname.casefold()
        payload["logger"] = record.name
        return json.dumps(redact_value(payload), default=str, ensure_ascii=False)


def apply_structlog_config() -> None:
    """Wire structlog (if present) to the stdlib logger without dup handlers."""
    if structlog is None:
        return
    try:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                renderer,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            cache_logger_on_first_use=True,
        )
    except Exception:
        pass


def configure(nested_capacity: int | None = None, *, quiet: bool = False) -> None:
    """Idempotent JSON logging init for workers/servers (Profile A minimum)."""
    logger = logging.getLogger("botapp.observability")
    logger.propagate = False
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonStdlibFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if quiet else logging.INFO)
    apply_structlog_config()
    if nested_capacity:
        RING.capacity = max(100, min(int(nested_capacity), 20000))


def parse_level(name: str) -> int:
    return _LEVEL_BY_NAME.get((name or "").casefold(), logging.INFO)
