"""
sandbox_runner.py — executed inside the Docker sandbox container.

Protocol (over stdio):
  Container stdout: __CALL__:<single-line JSON>
  Container stdin:  __RESULT__:<single-line JSON>

When user code calls e.g. crm.lookup_customer(keyword="Jane"), the stub:
  1. Prints __CALL__:{"ns":"crm","fn":"lookup_customer","kwargs":{"keyword":"Jane"}}
  2. Reads one __RESULT__:... line from stdin
  3. Returns the decoded result to the user code

All other print() output goes to stdout as normal — the host accumulates it.
"""

import base64
import json
import os
import sys

_CALL_PREFIX = "__CALL__:"
_RESULT_PREFIX = "__RESULT__:"

# Namespace → allowed method names — passed in by the host via NAMESPACE_METHODS env var
_NAMESPACE_METHODS: dict[str, list[str]] = json.loads(os.environ.get("NAMESPACE_METHODS", "{}"))


def _make_method(ns: str, fn: str):
    """Return a stub callable that serializes the call to the host and waits for the result."""
    def method(**kwargs):
        payload = json.dumps({"ns": ns, "fn": fn, "kwargs": kwargs})
        sys.stdout.write(_CALL_PREFIX + payload + "\n")
        sys.stdout.flush()

        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("Host closed stdin before returning a result")
        line = line.rstrip("\n")
        if not line.startswith(_RESULT_PREFIX):
            raise RuntimeError(f"Unexpected host response: {line!r}")
        data = json.loads(line[len(_RESULT_PREFIX):])
        if isinstance(data, dict) and "__error__" in data:
            raise RuntimeError(data["__error__"])
        return data
    return method


class _Namespace:
    def __init__(self, ns: str, methods: list[str]):
        for m in methods:
            setattr(self, m, _make_method(ns, m))


def _safe_builtins() -> dict:
    return {
        "print": print, "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter, "sorted": sorted,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "str": str, "int": int, "float": float, "bool": bool,
        "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
        "isinstance": isinstance, "repr": repr,
        "json": json,
    }


def main() -> None:
    code_b64 = os.environ.get("SANDBOX_CODE", "")
    if not code_b64:
        print("Error: SANDBOX_CODE env var not set", file=sys.stderr)
        sys.exit(1)

    code = base64.b64decode(code_b64).decode()
    allowed_tools: list[str] = json.loads(os.environ.get("ALLOWED_TOOLS", "[]"))

    globs: dict = {"__builtins__": _safe_builtins()}
    for ns_name in allowed_tools:
        if ns_name in _NAMESPACE_METHODS:
            globs[ns_name] = _Namespace(ns_name, _NAMESPACE_METHODS[ns_name])

    try:
        exec(code, globs)  # noqa: S102
    except SystemExit:
        pass
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
