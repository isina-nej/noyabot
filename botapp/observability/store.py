"""Bounded, private, cross-process telemetry and diagnostic queue storage.

SQLite here is *not* the application's Django database.  All payloads must be
redacted before being written.  The bot, private ASGI service and dedicated
diagnostic worker share NOYA_OPS_STATE_DIR on the same host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from .redaction import redact_value


def db_path() -> Path:
    root = Path(os.getenv("NOYA_OPS_STATE_DIR", str(Path.home() / ".local/state/noyaops"))).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError("ops state root must not be a symlink")
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / "ops.sqlite3"


def _connect():
    path = db_path()
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        level TEXT NOT NULL,
        service TEXT NOT NULL,
        trace_id TEXT,
        correlation_id TEXT,
        payload TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE INDEX IF NOT EXISTS events_trace ON events(trace_id,seq);
    CREATE TABLE IF NOT EXISTS diagnostic_runs (
        run_id TEXT PRIMARY KEY,
        test_id TEXT NOT NULL,
        actor_ref TEXT NOT NULL,
        status TEXT NOT NULL,
        outcome TEXT,
        result_json TEXT,
        error_type TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL
    );
    CREATE INDEX IF NOT EXISTS diagnostic_status ON diagnostic_runs(status,created_at);
    CREATE TABLE IF NOT EXISTS heartbeat (
        service TEXT PRIMARY KEY,
        pid INTEGER NOT NULL,
        seen_at REAL NOT NULL,
        git_commit TEXT NOT NULL
    );
    """)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


def record_event(entry: dict) -> bool:
    try:
        clean = redact_value(entry)
        # Never persist unstructured/raw conversations, even from a new caller.
        disallowed = {"prompt", "raw_prompt", "message", "content", "caption", "text", "reasoning", "thinking"}
        clean = {k: v for k, v in clean.items() if k not in disallowed}
        data = json.dumps(clean, ensure_ascii=False, default=str)[:16000]
        with _connect() as db:
            db.execute(
                "INSERT INTO events(ts,level,service,trace_id,correlation_id,payload) VALUES(?,?,?,?,?,?)",
                (time.time(), str(clean.get("level", "info"))[:20],
                 str(clean.get("service", "noyabot"))[:80], str(clean.get("trace_id") or "")[:64],
                 str(clean.get("correlation_id") or "")[:64], data),
            )
            # Amortized bounded pruning; no recurring service required.
            seq = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if seq % 128 == 0:
                max_rows = max(1000, min(int(os.getenv("NOYA_OPS_LOG_MAX_ROWS", "50000")), 250000))
                days = max(1, min(int(os.getenv("NOYA_OPS_LOG_RETENTION_DAYS", "7")), 90))
                db.execute("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))
                db.execute("DELETE FROM events WHERE seq <= ?", (max(0, seq - max_rows),))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def query_events(*, cursor=0, limit=100, level="", service="", trace_id="", correlation_id="", since=None, until=None):
    limit = max(1, min(int(limit), 200))
    cursor = max(0, int(cursor))
    conditions = ["seq > ?"] if cursor else []
    params = [cursor] if cursor else []
    for field, val in (("level", level), ("service", service), ("trace_id", trace_id), ("correlation_id", correlation_id)):
        if val:
            conditions.append(f"{field} = ?")
            params.append(str(val)[:80])
    if since is not None:
        conditions.append("ts >= ?")
        params.append(float(since))
    if until is not None:
        conditions.append("ts <= ?")
        params.append(float(until))
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    # Initial query shows the *latest* events, subsequent queries walk forward.
    order = "ASC" if cursor else "DESC"
    with _connect() as db:
        rows = db.execute(f"SELECT seq,payload FROM events{where} ORDER BY seq {order} LIMIT ?", [*params, limit]).fetchall()
        if not cursor:
            rows = list(reversed(rows))
    entries = []
    for row in rows:
        item = json.loads(row["payload"])
        item["_seq"] = row["seq"]
        entries.append(item)
    next_cursor = rows[-1]["seq"] if rows else cursor
    return entries, next_cursor


def create_run(test_id: str, actor_ref: str):
    run_id = secrets.token_hex(16)
    now = time.time()
    with _connect() as db:
        db.execute(
            "INSERT INTO diagnostic_runs(run_id,test_id,actor_ref,status,created_at) VALUES(?,?,?,?,?)",
            (run_id, test_id, actor_ref[:64], "queued", now),
        )
    return {"run_id": run_id, "test_id": test_id, "status": "queued", "created_at": now}


def get_run(run_id: str):
    with _connect() as db:
        row = db.execute("SELECT * FROM diagnostic_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json") or "{}")
    result.pop("actor_ref", None)
    return result


def run_events(run_id: str):
    run = get_run(run_id)
    if run is None:
        return None
    return [{"status": run["status"], "created_at": run["created_at"],
             "started_at": run["started_at"], "completed_at": run["completed_at"],
             "outcome": run["outcome"], "error_type": run["error_type"]}]


def claim_run(*, stale_after=60):
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        # Recover abandoned runs after a worker crash.
        db.execute("UPDATE diagnostic_runs SET status='failed',outcome='failed',error_type='WorkerLost',completed_at=?"
                   " WHERE status='running' AND started_at < ?", (now, now - stale_after))
        row = db.execute("SELECT run_id,test_id FROM diagnostic_runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE diagnostic_runs SET status='running',started_at=? WHERE run_id=?", (now, row["run_id"]))
        db.execute("DELETE FROM diagnostic_runs WHERE created_at < ?", (now - 7 * 86400,))
        return dict(row) if row else None


def complete_run(run_id: str, *, outcome: str, result: dict, error_type=""):
    clean = redact_value(result)
    with _connect() as db:
        db.execute(
            "UPDATE diagnostic_runs SET status='completed',outcome=?,result_json=?,error_type=?,completed_at=? WHERE run_id=? AND status='running'",
            (outcome, json.dumps(clean, ensure_ascii=False, default=str)[:8000], error_type[:80], time.time(), run_id),
        )


def write_heartbeat(service="telegram-worker"):
    from .logging import resolve_git_commit
    with _connect() as db:
        db.execute("INSERT INTO heartbeat(service,pid,seen_at,git_commit) VALUES(?,?,?,?) "
                   "ON CONFLICT(service) DO UPDATE SET pid=excluded.pid,seen_at=excluded.seen_at,git_commit=excluded.git_commit",
                   (service[:48], os.getpid(), time.time(), resolve_git_commit()))


def read_heartbeat(service="telegram-worker"):
    with _connect() as db:
        row = db.execute("SELECT pid,seen_at,git_commit FROM heartbeat WHERE service=?", (service,)).fetchone()
    if row is None:
        return {"healthy": False, "reason": "never_seen"}
    age = max(0, time.time() - row["seen_at"])
    return {"healthy": age <= 90, "age_seconds": round(age, 1),
            "pid": row["pid"], "git_commit": row["git_commit"]}
