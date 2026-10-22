"""Private Noya diagnostics CLI: JSON snapshots, bounded polling, no remote shell.

Use SSH forwarding/Tailscale, store NOYAOPS_API_KEY outside shell history.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class OpsError(RuntimeError):
    pass


def _parser():
    parser = argparse.ArgumentParser(prog="noyaops", description="Noya private observability client")
    parser.add_argument("--url", default=os.getenv("NOYAOPS_URL", "http://127.0.0.1:8000/api/internal/ops/v1"))
    parser.add_argument("--api-key", default=os.getenv("NOYAOPS_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NOYAOPS_TIMEOUT", "8")))
    parser.add_argument("--json", action="store_true", dest="as_json")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "health", "runtime", "metrics", "diagnostics", "doctor"):
        sub.add_parser(name)
    logs = sub.add_parser("logs")
    logs.add_argument("--limit", type=int, default=50)
    logs.add_argument("--level", default="")
    logs.add_argument("--service", default="")
    logs.add_argument("--trace-id", default="")
    logs.add_argument("--correlation-id", default="")
    logs.add_argument("--since", type=float, default=None, help="UTC Unix timestamp")
    logs.add_argument("--until", type=float, default=None, help="UTC Unix timestamp")
    logs.add_argument("--cursor", type=int, default=0)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--poll-seconds", type=float, default=2.0)
    trace = sub.add_parser("trace")
    trace.add_argument("trace_id")
    tests = sub.add_parser("tests")
    tests_sub = tests.add_subparsers(dest="tests_command", required=True)
    tests_sub.add_parser("list")
    run = tests_sub.add_parser("run")
    run.add_argument("test_id")
    for name in ("result", "events", "report"):
        item = tests_sub.add_parser(name)
        item.add_argument("run_id")
    return parser


class Client:
    def __init__(self, base: str, key: str, timeout: float):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise OpsError("invalid NOYAOPS_URL")
        # Explicit allow for SSH loopback HTTP; remote addresses require TLS.
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise OpsError("HTTP is allowed only over localhost/SSH; use HTTPS for remote access")
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = max(0.5, min(float(timeout), 60.0))

    def get(self, path, query=None):
        return self.request("GET", path, query=query)

    def post(self, path, body):
        return self.request("POST", path, body=body)

    def request(self, method, path, *, query=None, body=None):
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
        headers = {"Accept": "application/json", "User-Agent": "noyaops/2"}
        if self.key:
            headers["X-Noya-Ops-Key"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(3 if method == "GET" else 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = response.read(1_000_001)
                    if len(payload) > 1_000_000:
                        raise OpsError("server response exceeded 1MB")
                    return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if method == "GET" and exc.code in (502, 503, 504) and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                try:
                    detail = json.loads(exc.read(32000).decode("utf-8"))
                except Exception:
                    detail = {"error": f"HTTP {exc.code}"}
                raise OpsError(str(detail.get("error", f"HTTP {exc.code}"))) from exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                if method == "GET" and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise OpsError(f"ops API unavailable: {type(exc).__name__}") from exc
        raise OpsError("request retries exhausted")


def _logs_query(args, cursor=None):
    return {
        "limit": max(1, min(args.limit, 200)),
        "cursor": args.cursor if cursor is None else cursor,
        "level": args.level,
        "service": args.service,
        "trace_id": args.trace_id,
        "correlation_id": args.correlation_id,
        "since": args.since,
        "until": args.until,
    }


def _fetch(client, args):
    cmd = args.command
    if cmd in ("status", "health"):
        return client.get("health/")
    if cmd == "runtime":
        return client.get("runtime/")
    if cmd == "metrics":
        return client.get("metrics/summary/")
    if cmd == "diagnostics":
        return client.get("diagnostics/")
    if cmd == "doctor":
        return {
            "health": client.get("health/"),
            "runtime": client.get("runtime/"),
            "tests": client.get("diagnostics/tests/"),
        }
    if cmd == "logs":
        return client.get("logs/", _logs_query(args))
    if cmd == "trace":
        return client.get(f"traces/{urllib.parse.quote(args.trace_id, safe='')}/")
    if cmd == "tests":
        if args.tests_command == "list":
            return client.get("diagnostics/tests/")
        if args.tests_command == "run":
            return client.post("diagnostics/runs/", {"test_id": args.test_id})
        suffix = "" if args.tests_command == "result" else args.tests_command + "/"
        return client.get(f"diagnostics/runs/{urllib.parse.quote(args.run_id, safe='')}/{suffix}")
    raise OpsError(f"unsupported command: {cmd}")


def _pretty(value, indent=0):
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


def _output(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, default=str))
    else:
        _pretty(value)


def _follow(client, args):
    cursor = max(0, args.cursor)
    interval = max(0.5, min(args.poll_seconds, 30.0))
    try:
        while True:
            data = client.get("logs/", _logs_query(args, cursor))
            for item in data.get("entries", []):
                _output(item, args.as_json)
            cursor = max(cursor, int(data.get("next_cursor") or cursor))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    # Accept global options after a subcommand.
    global_opts, rest = [], []
    index = 0
    while index < len(raw):
        item = raw[index]
        if item == "--json":
            global_opts.append(item)
        elif item in ("--url", "--api-key", "--timeout") and index + 1 < len(raw):
            global_opts.extend(raw[index:index + 2])
            index += 1
        else:
            rest.append(item)
        index += 1
    args = _parser().parse_args(global_opts + rest)
    try:
        client = Client(args.url, args.api_key, args.timeout)
        if args.command == "logs" and args.follow:
            return _follow(client, args)
        result = _fetch(client, args)
    except OpsError as exc:
        print(f"noyaops: {exc}", file=sys.stderr)
        return 2
    _output(result, args.as_json)
    return 1 if isinstance(result, dict) and (
        result.get("outcome") == "failed" or result.get("status") == "degraded"
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
