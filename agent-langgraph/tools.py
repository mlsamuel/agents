"""
tools.py — LangChain @tool decorated functions for the support backend.

This is the KEY ARCHITECTURAL DIFFERENTIATOR from agent-mcp and agent-cli:

  agent-mcp:  tools are Python functions exposed via FastMCP over HTTP
  agent-cli:  tools are Click CLI subcommands invoked via subprocess.run()
  agent-langgraph: tools are @tool decorated Python functions executed
                   in-process by LangGraph's ToolNode — no server, no subprocess.

Tool implementations are identical to agent-mcp/mcp_server.py; only the
decorator changes (@mcp.tool() → @tool).

Since the graph is run via ainvoke(), ToolNode supports async tools natively.
The KB tools (search_knowledge_base, search_agent_guidelines) are async because
store.py uses asyncpg.
"""

import random
import string
from datetime import datetime, timedelta

from langchain_core.tools import tool

import store as kb

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
]

TOOLS_BY_NAME: dict[str, object] = {t.name: t for t in ALL_TOOLS}
