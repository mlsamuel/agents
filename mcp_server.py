"""
mcp_server.py - Simulated customer support backend via FastMCP (stdio transport).

Tools available to workflow agents:
  lookup_customer       - find customer by name/subject keyword
  get_ticket_history    - past tickets for a customer
  create_ticket         - open a new support ticket
  check_order_status    - get order info
  process_refund        - issue a refund
  escalate_to_human     - hand off to a human agent
  send_reply            - send a reply to the customer
  run_code              - execute sandboxed Python with access to approved tool namespaces

Run standalone (for testing):
  conda run -n base python mcp_server.py
"""

from logger import get_logger  # also silences third-party loggers on import

log = get_logger(__name__)

import ast
import io
import json
import random
import signal
import string
import sys
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SupportBackend")

# ── helpers ────────────────────────────────────────────────────────────────────

def _ticket_id() -> str:
    return "TKT-" + "".join(random.choices(string.digits, k=6))

def _order_id() -> str:
    return "ORD-" + "".join(random.choices(string.digits, k=8))

def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

# ── tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def lookup_customer(keyword: str) -> dict:
    """Look up a customer record by name or subject keyword. Returns customer profile."""
    return {
        "customer_id": "CUST-" + "".join(random.choices(string.digits, k=5)),
        "name": "Alex Morgan",
        "email": "alex.morgan@example.com",
        "account_tier": random.choice(["standard", "premium", "enterprise"]),
        "since": _days_ago(random.randint(100, 1000)),
        "keyword_matched": keyword,
    }


@mcp.tool()
def get_ticket_history(customer_id: str) -> list[dict]:
    """Retrieve the last 3 support tickets for a customer."""
    queues = ["Technical Support", "Billing and Payments", "Returns and Exchanges"]
    statuses = ["resolved", "closed", "open"]
    return [
        {
            "ticket_id": _ticket_id(),
            "subject": f"Issue #{i+1}",
            "queue": random.choice(queues),
            "status": random.choice(statuses),
            "created": _days_ago(random.randint(5, 90)),
        }
        for i in range(3)
    ]


@mcp.tool()
def create_ticket(
    subject: str,
    body: str,
    queue: str,
    priority: str,
    ticket_type: str,
) -> dict:
    """Create a new support ticket in the system. Returns the new ticket ID."""
    tid = _ticket_id()
    return {
        "ticket_id": tid,
        "subject": subject,
        "queue": queue,
        "priority": priority,
        "type": ticket_type,
        "status": "open",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "url": f"https://support.example.com/tickets/{tid}",
    }


@mcp.tool()
def check_order_status(order_ref: str) -> dict:
    """Look up the status of an order by reference number or keyword."""
    statuses = ["delivered", "shipped", "processing", "cancelled", "return_initiated"]
    status = random.choice(statuses)
    return {
        "order_id": order_ref if order_ref.startswith("ORD-") else _order_id(),
        "status": status,
        "items": [{"sku": "PROD-001", "qty": 1, "price": 49.99}],
        "total": 49.99,
        "ordered": _days_ago(random.randint(2, 30)),
        "estimated_delivery": _days_ago(-random.randint(1, 5)) if status == "shipped" else None,
    }


@mcp.tool()
def process_refund(order_ref: str, reason: str) -> dict:
    """Initiate a refund for an order. Returns refund confirmation."""
    return {
        "refund_id": "REF-" + "".join(random.choices(string.digits, k=7)),
        "order_ref": order_ref,
        "amount": 49.99,
        "reason": reason,
        "status": "approved",
        "expected_days": 5,
        "message": "Refund approved and will appear in 3-5 business days.",
    }


@mcp.tool()
def escalate_to_human(ticket_id: str, reason: str) -> dict:
    """Escalate a ticket to a human agent. Use when issue is complex or customer is frustrated."""
    return {
        "ticket_id": ticket_id,
        "escalated": True,
        "assigned_to": "Senior Support Team",
        "reason": reason,
        "eta": "2 business hours",
        "message": "Ticket escalated. A specialist will contact the customer shortly.",
    }


@mcp.tool()
def send_reply(ticket_id: str, message: str) -> dict:
    """Send a reply message to the customer on a ticket."""
    return {
        "ticket_id": ticket_id,
        "sent": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview": message[:120] + ("..." if len(message) > 120 else ""),
    }


# ── sandboxed code execution ────────────────────────────────────────────────────

_BLOCKED_MODULES = frozenset({
    "os", "sys", "subprocess", "multiprocessing", "threading", "concurrent",
    "signal", "ctypes", "socket", "ssl", "http", "urllib", "requests", "httpx",
    "aiohttp", "importlib", "pkgutil", "runpy", "code", "builtins", "types",
    "gc", "inspect", "pickle", "shelve", "sqlite3", "pathlib", "shutil",
    "glob", "tempfile", "pty", "mmap",
})

_SAFE_BUILTINS = {
    "print": print, "len": len, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "sorted": sorted,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "str": str, "int": int, "float": float, "bool": bool,
    "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
    "isinstance": isinstance, "repr": repr,
    "json": json,
}


def _ast_check(source: str) -> str | None:
    """Return an error string if the source contains blocked patterns, else None."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"SyntaxError: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Import of '{alias.name}' is not allowed"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Import from '{node.module}' is not allowed"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return f"Access to dunder attribute '{node.attr}' is not allowed"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "compile"):
                return f"Call to '{node.func.id}()' is not allowed"

    return None


def _make_namespace(fns: dict):
    """Create a simple namespace object where each key becomes an attribute."""
    ns = type("Namespace", (), {})()
    for name, fn in fns.items():
        setattr(ns, name, fn)
    return ns


# Maps namespace name → {method: backend_function}
# Each namespace groups existing MCP tool functions under a logical integration name.
_TOOL_REGISTRY: dict[str, dict] = {
    "crm": {
        "lookup_customer": lookup_customer,
        "get_ticket_history": get_ticket_history,
    },
    "orders": {
        "check_order_status": check_order_status,
        "process_refund": process_refund,
    },
    "tickets": {
        "create_ticket": create_ticket,
    },
    "comms": {
        "send_reply": send_reply,
        "escalate_to_human": escalate_to_human,
    },
}


def _timeout_handler(signum, frame):
    raise TimeoutError("Code execution timed out")


@mcp.tool()
def run_code(code: str, allowed_tools: list[str], timeout: int = 10) -> dict:
    """
    Execute a sandboxed Python snippet with access to approved tool namespaces.

    Available namespaces (specify in allowed_tools):
      crm     - crm.lookup_customer(keyword), crm.get_ticket_history(customer_id)
      orders  - orders.check_order_status(order_ref), orders.process_refund(order_ref, reason)
      tickets - tickets.create_ticket(subject, body, queue, priority, ticket_type)
      comms   - comms.send_reply(ticket_id, message), comms.escalate_to_human(ticket_id, reason)

    The code runs with restricted builtins — no imports, no filesystem, no network.
    Use print() to produce output; the captured stdout is returned.

    Example:
      code = \"\"\"
      customer = crm.lookup_customer(keyword="Jane")
      orders_list = crm.get_ticket_history(customer_id=customer["customer_id"])
      for t in orders_list:
          print(t["ticket_id"], t["status"])
      \"\"\"
      allowed_tools = ["crm"]
    """
    timeout = max(1, min(timeout, 30))

    rejection = _ast_check(code)
    if rejection:
        return {"stdout": "", "error": rejection, "exit_code": -1}

    # Build safe globals: restricted builtins + requested tool namespaces
    safe_globals = {"__builtins__": _SAFE_BUILTINS}
    for ns_name in allowed_tools:
        if ns_name in _TOOL_REGISTRY:
            safe_globals[ns_name] = _make_namespace(_TOOL_REGISTRY[ns_name])

    # Capture stdout and enforce wall-clock timeout via SIGALRM
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)

    try:
        exec(code, safe_globals)  # noqa: S102
        exit_code = 0
        error = None
    except TimeoutError as e:
        exit_code = -1
        error = str(e)
    except Exception as e:
        exit_code = -1
        error = f"{type(e).__name__}: {e}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        sys.stdout = old_stdout

    return {
        "stdout": captured.getvalue()[:8192],
        "error": error,
        "exit_code": exit_code,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
