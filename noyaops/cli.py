"""The ``noyaops`` operator CLI.

The CLI deliberately uses only the Python standard library.  It talks to the
authenticated private Django API and never executes a remote command.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class OpsError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noyaops", description="Noya private observability client")
    parser.add_argument("--url", default=os.getenv("NOYAOPS_URL", "http://127.0.0.1:8000/api/internal/ops/v1"))
    parser.add_argument("--api-key", default=os.getenv("NOYAOPS_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NOYAOPS_TIMEOUT", "8")))
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "health", "runtime", "metrics", "diagnostics", "doctor"):
        sub.add_parser(name)
    logs = sub.add_parser("logs")
    logs.add_argument("--limit", type=int, default=50)
    logs.add_argument("--level", default="")
    logs.add_argument("--trace-id", default="")
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


class Client:
    def __init__(self, base: str, key: str, timeout: float):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = max(0.5, min(timeout, 60.0))

    def get(self, path: str, query: dict | None = None):
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body=body)

    def request(self, method: str, path: str, *, query=None, body=None):
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        headers = {"Accept": "application/json", "User-Agent": "noyaops/1"}
        if self.key:
            headers["X-API-Key"] = self.key
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(1_000_001)
                if len(payload) > 1_000_000:
                    raise OpsError("server response exceeded the 1MB safety limit")
                return json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(32_000).decode("utf-8"))
            except Exception:
                detail = {"error": f"HTTP {exc.code}"}
            raise OpsError(str(detail.get("error", f"HTTP {exc.code}"))) from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise OpsError(f"ops API unavailable: {type(exc).__name__}") from exc


def _fetch(client: Client, args):
    command = args.command
    if command in {"status", "health"}:
        return client.get("health/")
    if command == "runtime":
        return client.get("runtime/")
    if command == "metrics":
        return client.get("metrics/summary/")
    if command == "diagnostics":
        return client.get("diagnostics/")
    if command == "doctor":
        health = client.get("health/")
        runtime = client.get("runtime/")
        tests = client.get("tests/")
        return {"health": health, "runtime": runtime, "tests": tests}
    if command == "logs":
        return client.get("logs/", {"limit": max(1, min(args.limit, 200)), "level": args.level, "trace_id": args.trace_id})
    if command == "trace":
        return client.get(f"traces/{urllib.parse.quote(args.trace_id, safe='')}/")
    if command == "tests":
        if args.tests_command == "list":
            return client.get("tests/")
        if args.tests_command == "run":
            return client.post("diagnostics/", {"test_id": args.test_id})
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


def main(argv=None) -> int:
    # Accept global options before or after the subcommand, as operators tend
    # to type both ``noyaops --json health`` and ``noyaops health --json``.
    raw = list(sys.argv[1:] if argv is None else argv)
    global_opts = []
    rest = []
    i = 0
    while i < len(raw):
        item = raw[i]
        if item == "--json":
            global_opts.append(item)
        elif item in {"--url", "--api-key", "--timeout"} and i + 1 < len(raw):
            global_opts.extend([item, raw[i + 1]])
            i += 1
        else:
            rest.append(item)
        i += 1
    args = _parser().parse_args(global_opts + rest)
    try:
        result = _fetch(Client(args.url, args.api_key, args.timeout), args)
    except OpsError as exc:
        print(f"noyaops: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _pretty(result)
    if isinstance(result, dict) and result.get("outcome") == "failed":
        return 1
    return 0
