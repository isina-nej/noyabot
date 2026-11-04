"""Restricted Python sandbox for agent-side data analysis.

The model may emit short analysis snippets that operate on an in-memory
``data`` dict gathered from allowlisted tools. This is intentionally NOT a
general code-execution environment:

* no imports / network / filesystem / process access
* tiny safe builtin set + ``math`` / ``statistics`` / ``json`` modules
* hard wall-clock timeout via an external killable process
* source rejected if it contains obvious escape patterns
"""

from __future__ import annotations

import ast
import json
import math
import statistics
from dataclasses import dataclass
from typing import Any

_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.With,
    ast.AsyncWith,
    ast.Raise,
    ast.Try,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Global,
    ast.Nonlocal,
    ast.Lambda,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
)

_FORBIDDEN_NAMES = frozenset({
    "__import__",
    "open",
    "exec",
    "eval",
    "compile",
    "input",
    "breakpoint",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    "memoryview",
    "classmethod",
    "staticmethod",
    "property",
    "type",
    "object",
    "super",
    "help",
    "exit",
    "quit",
})

_FORBIDDEN_ATTR_PREFIXES = ("__",)


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    result: Any = None
    stdout: str = ""
    error: str = ""


class _SafePrinter:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, *args, **kwargs):
        sep = kwargs.get("sep", " ")
        self.lines.append(sep.join(str(a) for a in args))


def _validate_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise ValueError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"disallowed name: {node.id}")
        if isinstance(node, ast.Attribute):
            attr = node.attr or ""
            if any(attr.startswith(prefix) for prefix in _FORBIDDEN_ATTR_PREFIXES):
                raise ValueError(f"disallowed attribute: {attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_NAMES:
                raise ValueError(f"disallowed call: {node.func.id}")


def _safe_builtins(printer: _SafePrinter) -> dict[str, Any]:
    return {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": printer,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "True": True,
        "False": False,
        "None": None,
    }


def _execute(code: str, data: dict[str, Any]) -> SandboxResult:
    try:
        tree = ast.parse(code, mode="exec")
        _validate_ast(tree)
        compiled = compile(tree, "<agent-sandbox>", "exec")
    except Exception as exc:  # noqa: BLE001
        return SandboxResult(ok=False, error=f"compile_error: {exc}")

    printer = _SafePrinter()
    namespace: dict[str, Any] = {
        "__builtins__": _safe_builtins(printer),
        "data": data,
        "result": None,
        "math": math,
        "statistics": statistics,
        "json": json,
    }
    try:
        exec(compiled, namespace, namespace)  # noqa: S102 - intentional restricted exec
    except Exception as exc:  # noqa: BLE001
        return SandboxResult(
            ok=False,
            stdout="\n".join(printer.lines),
            error=f"runtime_error: {type(exc).__name__}: {exc}",
        )
    result = namespace.get("result")
    # Keep JSON-serialisable / printable only.
    try:
        json.dumps(result, ensure_ascii=False, default=str)
    except TypeError:
        result = str(result)
    return SandboxResult(ok=True, result=result, stdout="\n".join(printer.lines))


def run_sandboxed(
    code: str,
    data: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 2.0,
    max_chars: int = 4000,
) -> SandboxResult:
    source = (code or "").strip()
    if not source:
        return SandboxResult(ok=False, error="empty_code")
    if len(source) > max_chars:
        return SandboxResult(ok=False, error="code_too_long")
    lowered = source.casefold()
    for needle in ("import ", "__", "open(", "os.", "sys.", "subprocess", "socket", "pathlib", "builtins"):
        if needle in lowered or needle in source:
            return SandboxResult(ok=False, error=f"forbidden_pattern:{needle.strip()}")

    # A process, unlike a thread, can be killed and reaped on timeout.
    # Untrusted code is disabled unless explicitly opted in (CI sets
    # DEBUG=true for the bounded regression suite).
    import os
    import subprocess
    import sys
    from pathlib import Path

    enabled = os.getenv(
        "NOYA_SANDBOX_ENABLED", os.getenv("DEBUG", "false")
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if not enabled:
        return SandboxResult(ok=False, error="sandbox_disabled")

    payload = data if isinstance(data, dict) else {}
    try:
        request = json.dumps({"code": source, "data": payload}, default=str)
        if len(request.encode("utf-8")) > 1_000_000:
            return SandboxResult(ok=False, error="input_too_large")
    except (ValueError, TypeError):
        return SandboxResult(ok=False, error="invalid_data")

    worker = Path(__file__).with_name("_sandbox_worker.py")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(worker)],
            input=request, text=True, capture_output=True,
            cwd="/", env={"PYTHONIOENCODING": "utf-8"},
            timeout=max(0.1, float(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the worker on timeout.
        return SandboxResult(ok=False, error="timeout")
    except (OSError, ValueError) as exc:
        return SandboxResult(ok=False, error=f"sandbox_launch_error:{type(exc).__name__}")

    if completed.returncode != 0 or len(completed.stdout) > 32768:
        return SandboxResult(ok=False, error="sandbox_process_failed")
    try:
        response = json.loads(completed.stdout)
        if not isinstance(response, dict):
            return SandboxResult(ok=False, error="invalid_sandbox_output")
        return SandboxResult(
            ok=bool(response.get("ok")),
            result=response.get("result"),
            stdout=str(response.get("stdout", ""))[:4000],
            error=str(response.get("error", ""))[:500],
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return SandboxResult(ok=False, error="invalid_sandbox_output")
