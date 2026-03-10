"""
tools.py — LangChain @tool decorated functions for the support backend.

Tools are @tool decorated Python functions executed in-process by LangGraph's
ToolNode — no server, no subprocess.

Since the graph is run via ainvoke(), ToolNode supports async tools natively.
The KB tools (search_knowledge_base, search_agent_guidelines) are async because
store.py uses asyncpg.
"""

import asyncio
import base64
import json
import random
import string
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.tools import tool

import store as kb

# ── run_code sandbox config ────────────────────────────────────────────────────

_SANDBOX_RUNNER = Path(__file__).parent / "sandbox_runner.py"
_DOCKER_IMAGE   = "python:3.12-slim"

# Namespace → method names passed to the Docker sandbox via NAMESPACE_METHODS env var.
_NAMESPACE_METHODS: dict[str, list[str]] = {
    "crm":     ["lookup_customer", "get_ticket_history"],
    "orders":  ["check_order_status", "process_refund"],
    "tickets": ["create_ticket"],
    "comms":   ["send_reply", "escalate_to_human"],
    "kb":      ["search_knowledge_base", "search_agent_guidelines"],
}

# Populated after tool definitions below.
_TOOL_DISPATCH: dict[tuple[str, str], object] = {}

# ── helpers ────────────────────────────────────────────────────────────────────

def _ticket_id() -> str:
    return "TKT-" + "".join(random.choices(string.digits, k=6))


def _order_id() -> str:
    return "ORD-" + "".join(random.choices(string.digits, k=8))


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


_FIRST_NAMES = ["Jordan", "Taylor", "Morgan", "Riley", "Casey", "Quinn", "Avery",
                "Blake", "Drew", "Jamie", "Reese", "Skyler", "Parker", "Sage", "Robin"]
_LAST_NAMES  = ["Chen", "Patel", "Smith", "Garcia", "Kim", "Müller", "Okafor",
                "Nguyen", "Torres", "Eriksson", "Russo", "Yamamoto", "Singh", "Costa"]


def _customer_from_keyword(keyword: str) -> tuple[str, str]:
    """Derive a stable (first, last) name from the keyword so the same search
    always returns the same customer, but different keywords return different ones."""
    h = hash(keyword.lower().strip())
    first = _FIRST_NAMES[h % len(_FIRST_NAMES)]
    last  = _LAST_NAMES[(h // len(_FIRST_NAMES)) % len(_LAST_NAMES)]
    return first, last


# ── tools ──────────────────────────────────────────────────────────────────────

@tool
def lookup_customer(keyword: str) -> dict:
    """Look up a customer record by name or subject keyword. Returns customer profile."""
    first, last = _customer_from_keyword(keyword)
    rng = random.Random(hash(keyword.lower().strip()))
    return {
        "customer_id": "CUST-" + "".join(rng.choices(string.digits, k=5)),
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}@example.com",
        "account_tier": rng.choice(["standard", "premium", "enterprise"]),
        "since": _days_ago(rng.randint(100, 1000)),
        "keyword_matched": keyword,
    }


@tool
def get_ticket_history(customer_id: str) -> list:
    """Retrieve the last 3 support tickets for a customer."""
    queues = ["Technical Support", "Billing and Payments", "Returns and Exchanges"]
    statuses = ["resolved", "closed", "open"]
    return [
        {
            "ticket_id": _ticket_id(),
            "subject": f"Issue #{i + 1}",
            "queue": random.choice(queues),
            "status": random.choice(statuses),
            "created": _days_ago(random.randint(5, 90)),
        }
        for i in range(3)
    ]


@tool
def create_ticket(subject: str, body: str, queue: str, priority: str, ticket_type: str) -> dict:
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


@tool
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


@tool
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


@tool
def escalate_to_human(ticket_id: str, reason: str) -> dict:
    """Escalate a ticket to a human agent. Use when the issue is complex or the customer is frustrated."""
    return {
        "ticket_id": ticket_id,
        "escalated": True,
        "assigned_to": "Senior Support Team",
        "reason": reason,
        "eta": "2 business hours",
        "message": "Ticket escalated. A specialist will contact the customer shortly.",
    }


@tool
def send_reply(message: str, ticket_id: str = "") -> dict:
    """Send a reply message to the customer. ticket_id is optional — omit when no ticket has been created yet."""
    return {
        "ticket_id": ticket_id,
        "sent": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview": message[:120] + ("..." if len(message) > 120 else ""),
    }


@tool
async def search_knowledge_base(query: str, category: str = "", top_k: int = 3) -> list:
    """Search the support knowledge base for policy answers relevant to a query.

    Returns up to top_k entries with answer text and a relevance score (0–1).
    Use this before creating a ticket to check whether a direct answer exists.

    Args:
        query:    The customer's question or topic to look up.
        category: Optional filter — one of: billing, returns, technical, general.
                  Leave blank to search all categories.
        top_k:    Maximum number of results to return (default 3).
    """
    return await kb.search(query, category, top_k)


@tool
async def search_agent_guidelines(query: str, category: str = "") -> list:
    """Search agent handling guidelines for the current customer situation.

    Call this when you need to know what information to collect from the customer
    before acting — e.g. for billing disputes, technical investigations, or
    documentation requests. Returns instructions written for the agent.

    Args:
        query:    Description of the current customer situation.
        category: Optional filter — billing, returns, technical, general.
    """
    return await kb.search_guideline(query, category, top_k=3)


# ── run_code implementation ────────────────────────────────────────────────────

def _dispatch_to_langgraph(ns: str, fn: str, kwargs: dict) -> dict:
    """Dispatch a sandboxed __CALL__ to an in-process Python function.
    Called from the Docker stdout-reader thread — no running event loop here,
    so asyncio.run() is safe for async KB tools."""
    func = _TOOL_DISPATCH.get((ns, fn))
    if func is None:
        return {"__error__": f"Tool {ns}.{fn} not available"}
    try:
        result = func(**kwargs)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result
    except Exception as exc:
        return {"__error__": str(exc)}


def _run_code_docker(code: str, allowed_tools: list[str], timeout: int) -> dict:
    """Run code in an isolated Docker container, dispatching tool calls in-process."""
    code_b64 = base64.b64encode(code.encode()).decode()
    name = f"sandbox-{uuid.uuid4().hex[:8]}"

    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--network", "none",
        "--memory", "128m",
        "--cpus", "0.5",
        "--read-only",
        "--tmpfs", "/tmp:size=32m,noexec",
        "-e", f"SANDBOX_CODE={code_b64}",
        "-e", f"ALLOWED_TOOLS={json.dumps(allowed_tools)}",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", f"NAMESPACE_METHODS={json.dumps(_NAMESPACE_METHODS)}",
        "-v", f"{_SANDBOX_RUNNER.resolve()}:/runner.py:ro",
        _DOCKER_IMAGE,
        "python", "/runner.py",
    ]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return {"stdout": "", "error": "docker executable not found", "exit_code": -1}

    output_parts: list[str] = []
    timed_out = False

    def _kill_container():
        nonlocal timed_out
        timed_out = True
        subprocess.run(["docker", "kill", name], capture_output=True)

    timer = threading.Timer(timeout, _kill_container)
    timer.start()

    try:
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode(errors="replace")
            if line.startswith("__CALL__:"):
                try:
                    call = json.loads(line[9:])
                    result = _dispatch_to_langgraph(call["ns"], call["fn"], call["kwargs"])
                except Exception as exc:
                    result = {"__error__": str(exc)}
                proc.stdin.write(f"__RESULT__:{json.dumps(result)}\n".encode())
                proc.stdin.flush()
            else:
                output_parts.append(line)
    except BrokenPipeError:
        pass
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except Exception:
            pass

    proc.wait()
    exit_code = proc.returncode
    stderr_text = proc.stderr.read().decode(errors="replace").strip()

    if timed_out:
        error: str | None = f"Code execution timed out after {timeout}s"
        exit_code = -1
    elif exit_code != 0:
        error = stderr_text or f"Process exited with code {exit_code}"
    else:
        error = None

    return {"stdout": "".join(output_parts)[:8192], "error": error, "exit_code": exit_code}


@tool
def run_code(code: str, allowed_tools: list[str], timeout: int = 10) -> dict:
    """Execute a sandboxed Python snippet with access to approved tool namespaces.

    Runs in a Docker container (--network none, read-only rootfs, 128MB RAM, 0.5 CPU).
    Use print() to produce output — captured stdout is returned.

    Available namespaces (specify in allowed_tools):
      crm     - crm.lookup_customer(keyword), crm.get_ticket_history(customer_id)
      orders  - orders.check_order_status(order_ref), orders.process_refund(order_ref, reason)
      tickets - tickets.create_ticket(subject, body, queue, priority, ticket_type)
      comms   - comms.send_reply(message, ticket_id), comms.escalate_to_human(ticket_id, reason)
      kb      - kb.search_knowledge_base(query, category, top_k), kb.search_agent_guidelines(query, category)

    Args:
        code:          Python code to execute. Use print() to produce output.
        allowed_tools: Namespaces to expose, e.g. ["crm", "tickets"]
        timeout:       Max execution seconds (default 10, max 30)
    """
    timeout = max(1, min(timeout, 30))
    return _run_code_docker(code, allowed_tools, timeout)


# ── Registry ───────────────────────────────────────────────────────────────────

ALL_TOOLS = [
    lookup_customer,
    get_ticket_history,
    create_ticket,
    check_order_status,
    process_refund,
    escalate_to_human,
    send_reply,
    search_knowledge_base,
    search_agent_guidelines,
    run_code,
]

TOOLS_BY_NAME: dict[str, object] = {t.name: t for t in ALL_TOOLS}

# Dispatch table for the Docker sandbox bridge — populated here so all @tool
# objects are defined before we reference their .func / .coroutine attributes.
#
# KB tools need a fresh asyncpg pool because _run_code_docker runs in a thread
# pool executor with its own event loop (via asyncio.run()), separate from the
# main LangGraph loop where store._pool was originally created.
async def _kb_search(query: str, category: str = "", top_k: int = 3) -> list:
    kb._pool = None  # force pool recreation in this event loop
    return await kb.search(query, category, top_k)


async def _kb_guidelines(query: str, category: str = "") -> list:
    kb._pool = None  # force pool recreation in this event loop
    return await kb.search_guideline(query, category, top_k=3)


_TOOL_DISPATCH.update({
    ("crm",     "lookup_customer"):          lookup_customer.func,
    ("crm",     "get_ticket_history"):       get_ticket_history.func,
    ("orders",  "check_order_status"):       check_order_status.func,
    ("orders",  "process_refund"):           process_refund.func,
    ("tickets", "create_ticket"):            create_ticket.func,
    ("comms",   "send_reply"):               send_reply.func,
    ("comms",   "escalate_to_human"):        escalate_to_human.func,
    ("kb",      "search_knowledge_base"):    _kb_search,
    ("kb",      "search_agent_guidelines"):  _kb_guidelines,
})
