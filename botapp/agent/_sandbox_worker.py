"""Child process for limited Python data analysis.

Runs with -I -S, an empty credential environment, no Django imports and
resource limits. It is not a general-purpose security sandbox; production
execution stays disabled unless explicitly enabled by the operator.
"""
import json
import os
import sys

# Only our minimal sandbox module is importable, not the application package.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from sandbox import _execute  # noqa: E402


def main():
    try:
        import resource
        # Restrict resource consumption even if an AST check is circumvented.
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        data = sys.stdin.buffer.read(1_000_001)
        if len(data) > 1_000_000:
            raise ValueError("input_too_large")
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError("invalid_input")
        code = request.get("code", "")
        payload = request.get("data", {})
        if not isinstance(code, str) or len(code) > 4000 or not isinstance(payload, dict):
            raise ValueError("invalid_input")
        result = _execute(code, payload)
        response = {
            "ok": result.ok,
            "result": result.result,
            "stdout": result.stdout[:4000],
            "error": result.error[:500],
        }
        encoded = json.dumps(response, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 32768:
            raise ValueError("output_too_large")
    except Exception as exc:
        encoded = json.dumps({
            "ok": False, "result": None, "stdout": "",
            "error": "worker_error:" + type(exc).__name__,
        })
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
