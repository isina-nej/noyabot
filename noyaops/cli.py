"""Small, dependency-free client for Noya's private operations API.

The client intentionally uses only the standard library. It sends a fresh
request ID on every call, retries only safe GET requests, and never executes a
remote command. API keys should normally come from the environment or the
user's mode-600 TOML config file rather than a shell argument.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class OpsError(RuntimeError):
    """An expected local or remote operations error."""


DEFAULT_URL = "http://127.0.0.1:8000/api/internal/ops/v1"
DEFAULT_CONFIG = Path("~/.config/noyaops/config.toml").expanduser()
MAX_RESPONSE_BYTES = 1_000_000
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noyaops", description="Noya private observability client")
    parser.add_argument("--config", default=None, help="TOML config path (default: ~/.config/noyaops/config.toml)")
    parser.add_argument("--url", default=None, help="private operations API base URL")
    parser.add_argument("--api-key", default=None, help="API key (prefer NOYAOPS_API_KEY or config file)")
    parser.add_argument("--timeout", type=float, default=None, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=None, help="safe GET retries (default: 2)")
    parser.add_argument("--retry-backoff", type=float, default=None, help="initial retry delay in seconds")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "health", "runtime", "metrics", "diagnostics", "doctor"):
        sub.add_parser(name)
    logs = sub.add_parser("logs")
    logs.add_argument("--limit", type=int, default=50)
    logs.add_argument("--cursor", type=int, default=0)
    logs.add_argument("--level", default="")
    logs.add_argument("--service", default="")
    logs.add_argument("--trace-id", default="")
    logs.add_argument("--correlation-id", default="")
    logs.add_argument("--request-id", default="")
    logs.add_argument("--since", "--start", dest="since", default="", help="UTC timestamp lower bound")
    logs.add_argument("--until", "--end", dest="until", default="", help="UTC timestamp upper bound")
    logs.add_argument("--follow", action="store_true", help="poll for new entries until interrupted")
    logs.add_argument("--interval", type=float, default=None, help="follow poll interval in seconds")
    trace = sub.add_parser("trace")
    trace.add_argument("trace_id")
    tests = sub.add_parser("tests")
    tests_sub = tests.add_subparsers(dest="tests_command", required=True)
    tests_sub.add_parser("list")
    run = tests_sub.add_parser("run")
    run.add_argument("test_id")
    result = tests_sub.add_parser("result")
    result.add_argument("run_id")
    return parser


def _global_options(raw: list[str]) -> tuple[list[str], list[str]]:
    """Allow global options before or after a subcommand."""
    takes_value = {"--config", "--url", "--api-key", "--timeout", "--retries", "--retry-backoff"}
    global_opts: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(raw):
        item = raw[i]
        if item == "--json":
            global_opts.append(item)
        elif item in takes_value and i + 1 < len(raw):
            global_opts.extend([item, raw[i + 1]])
            i += 1
        else:
            rest.append(item)
        i += 1
    return global_opts, rest


def _config_path(raw: list[str]) -> Path:
    for i, item in enumerate(raw[:-1]):
        if item == "--config":
            return Path(raw[i + 1]).expanduser()
    configured = os.getenv("NOYAOPS_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG


def _load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OpsError(f"cannot read config {path}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise OpsError(f"config {path} must contain a TOML table")
    # Refuse to read a secret from a group/world-readable file.
    try:
        insecure = path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH)
    except OSError as exc:
        raise OpsError(f"cannot stat config {path}: {type(exc).__name__}") from exc
    if data.get("api_key") and insecure:
        raise OpsError(f"config {path} contains api_key; run chmod 600 {path}")
    return data


def _setting(value, config: dict, env_name: str, key: str, default):
    if value is not None:
        return value
    env_value = os.getenv(env_name)
    if env_value not in (None, ""):
        return env_value
    return config.get(key, default)


def _settings(args, config: dict) -> dict:
    try:
        timeout = float(_setting(args.timeout, config, "NOYAOPS_TIMEOUT", "timeout", 8.0))
        retries = int(_setting(args.retries, config, "NOYAOPS_RETRIES", "retries", 2))
        backoff = float(_setting(args.retry_backoff, config, "NOYAOPS_RETRY_BACKOFF", "retry_backoff", 0.5))
        interval = float(_setting(None, config, "NOYAOPS_POLL_INTERVAL", "poll_interval", 2.0))
    except (TypeError, ValueError) as exc:
        raise OpsError("timeout, retries, retry_backoff, and poll_interval must be numeric") from exc
    return {
        "url": str(_setting(args.url, config, "NOYAOPS_URL", "url", DEFAULT_URL)),
        "api_key": str(_setting(args.api_key, config, "NOYAOPS_API_KEY", "api_key", "")),
        "timeout": timeout,
        "retries": retries,
        "backoff": backoff,
        "interval": max(0.2, min(interval, 60.0)),
    }


def _validate_base_url(base: str) -> str:
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpsError("NOYAOPS_URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OpsError("NOYAOPS_URL must not contain credentials, query parameters, or fragments")
    return base.rstrip("/")


class Client:
    def __init__(self, base: str, key: str, timeout: float, retries: int = 2, retry_backoff: float = 0.5):
        self.base = _validate_base_url(base)
        self.key = key
        self.timeout = max(0.5, min(float(timeout), 60.0))
        self.retries = max(0, min(int(retries), 5))
        self.retry_backoff = max(0.05, min(float(retry_backoff), 5.0))

    def get(self, path: str, query: dict | None = None):
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body=body)

    def request(self, method: str, path: str, *, query=None, body=None):
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        headers = {
            "Accept": "application/json",
            "User-Agent": "noyaops/1",
            "X-Request-ID": uuid.uuid4().hex,
        }
        if self.key:
            headers["X-API-Key"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        safe_to_retry = method.upper() == "GET"
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise OpsError("server response exceeded the 1MB safety limit")
                    try:
                        return json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise OpsError("server returned invalid JSON") from exc
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                if safe_to_retry and exc.code in RETRYABLE_STATUS and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(_retry_delay(self.retry_backoff, attempt, retry_after))
                    continue
                raise OpsError(detail) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if safe_to_retry and attempt < self.retries:
                    time.sleep(_retry_delay(self.retry_backoff, attempt, None))
                    continue
                raise OpsError(f"ops API unavailable: {type(exc).__name__}") from exc
        raise OpsError("ops API request failed")


def _retry_delay(backoff: float, attempt: int, retry_after: str | None) -> float:
    try:
        if retry_after:
            return max(0.05, min(float(retry_after), 10.0))
    except ValueError:
        pass
    return min(backoff * (2**attempt), 10.0)


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(32_000).decode("utf-8", "replace")
        value = json.loads(raw)
        if isinstance(value, dict):
            message = value.get("error") or value.get("detail")
            if message:
                return f"HTTP {exc.code}: {str(message).replace(chr(10), ' ')[:240]}"
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        pass
    return f"HTTP {exc.code}"


def _fetch(client: Client, args):
    command = args.command
    if command == "status":
        return client.get("health/live/")
    if command == "health":
        return client.get("health/")
    if command == "runtime":
        return client.get("runtime/")
    if command == "metrics":
        return client.get("metrics/summary/")
    if command == "diagnostics":
        return client.get("diagnostics/")
    if command == "doctor":
        checks = {}
        for name, path in (("live", "health/live/"), ("ready", "health/ready/"),
                           ("dependencies", "health/dependencies/"), ("runtime", "runtime/"),
                           ("tests", "tests/")):
            try:
                checks[name] = client.get(path)
            except OpsError as exc:
                checks[name] = {"error": str(exc)}
        return checks
    if command == "logs":
        query = {
            "limit": max(1, min(args.limit, 200)),
            "cursor": max(0, args.cursor),
            "level": args.level,
            "service": args.service,
            "trace_id": args.trace_id,
            "correlation_id": args.correlation_id,
            "request_id": args.request_id,
            "since": args.since,
            "until": args.until,
        }
        return client.get("logs/", query)
    if command == "trace":
        return client.get(f"traces/{urllib.parse.quote(args.trace_id, safe='')}/")
    if command == "tests":
        if args.tests_command == "list":
            return client.get("tests/")
        if args.tests_command == "run":
            return client.post("diagnostics/run/", {"test_id": args.test_id})
        return client.get(f"diagnostics/runs/{urllib.parse.quote(args.run_id, safe='')}/")
    raise OpsError(f"unsupported command: {command}")


def _pretty(value, indent: int = 0):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(" " * indent + f"{key}:")
                _pretty(item, indent + 2)
            else:
                print(" " * indent + f"{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                print(" " * indent + "-")
                _pretty(item, indent + 2)
            else:
                print(" " * indent + f"- {item}")
    else:
        print(value)


def _follow_logs(client: Client, args, *, as_json: bool, interval: float) -> int:
    cursor = max(0, args.cursor)
    try:
        while True:
            query = {"limit": max(1, min(args.limit, 200)), "cursor": cursor,
                     "level": args.level, "service": args.service,
                     "trace_id": args.trace_id, "correlation_id": args.correlation_id,
                     "request_id": args.request_id, "since": args.since, "until": args.until}
            result = client.get("logs/", query)
            if as_json:
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            elif result.get("entries"):
                _pretty(result["entries"])
            cursor = max(cursor, int(result.get("next_cursor", cursor)))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _has_failure(value) -> bool:
    if isinstance(value, dict):
        if value.get("outcome") == "failed" or value.get("status") in {"failed", "degraded", "unhealthy", "error"}:
            return True
        return any(_has_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_failure(item) for item in value)
    return False


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = None
    wants_json = "--json" in raw
    try:
        config = _load_config(_config_path(raw))
        global_opts, rest = _global_options(raw)
        args = _parser().parse_args(global_opts + rest)
        settings = _settings(args, config)
        client = Client(settings["url"], settings["api_key"], settings["timeout"], settings["retries"], settings["backoff"])
        if args.command == "logs" and args.follow:
            interval = settings["interval"] if args.interval is None else max(0.2, min(args.interval, 60.0))
            return _follow_logs(client, args, as_json=args.as_json, interval=interval)
        result = _fetch(client, args)
    except (OpsError, ValueError) as exc:
        if wants_json or getattr(args, "as_json", False):
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"noyaops: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _pretty(result)
    return 1 if _has_failure(result) else 0
