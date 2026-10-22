"""Read-only health probes used by the private ops API and layers below."""
from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime, timezone


def process_uptime_seconds() -> float:
    try:
        with open("/proc/self/stat", "rb") as fh:
            btime_ticks = float(fh.read().split()[21])
    except Exception:
        return 0.0
    ticks = os.sysconf("SC_CLK_TCK") or 100
    with open("/proc/stat", "rb") as fh:
        boot_line = None
        for line in fh:
            if line.startswith(b"btime "):
                boot_line = float(line.split()[1])
                break
    if boot_line and btime_ticks:
        boot_seconds = boot_line + btime_ticks / ticks
        return max(0.0, time.time() - boot_seconds)
    return 0.0


def rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    """Bounded TCP probe. No data enters the connection (read-only smoke)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except Exception as exc:
        return False, type(exc).__name__


def db_ready() -> tuple[bool, str]:
    from django.db import connections

    conn = connections["default"]
    try:
        conn.ensure_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True, ""
    except Exception as exc:
        return False, type(exc).__name__


def system_facts() -> dict:
    facts: dict = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pid": os.getpid(),
        "now": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
    }
    rss = rss_mb()
    if rss is not None:
        facts["rss_mb"] = round(rss, 1)
    return facts
