"""
tools.py - Pure tool implementations for the support agent CLI.

These functions contain the actual business logic, callable independently
with no framework required.

Sync tools:  lookup_customer, get_ticket_history, create_ticket,
             check_order_status, process_refund, escalate_to_human, send_reply
Async tools: search_knowledge_base, search_agent_guidelines  (require DB pool)
"""

import random
import string
from datetime import datetime, timedelta

import store as kb

# ── helpers ─────────────────────────────────────────────────────────────────────

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
    h = hash(keyword.lower().strip())
    first = _FIRST_NAMES[h % len(_FIRST_NAMES)]
    last  = _LAST_NAMES[(h // len(_FIRST_NAMES)) % len(_LAST_NAMES)]
    return first, last


# ── sync tools ──────────────────────────────────────────────────────────────────

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


def send_reply(message: str, ticket_id: str = "") -> dict:
    """Send a reply message to the customer. ticket_id is optional."""
    return {
        "ticket_id": ticket_id,
        "sent": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview": message[:120] + ("..." if len(message) > 120 else ""),
    }


# ── async tools (require DB pool via store) ─────────────────────────────────────

async def search_knowledge_base(query: str, category: str = "", top_k: int = 3) -> list[dict]:
    """Search the support knowledge base for policy answers relevant to a query.

    Returns up to top_k entries with answer text and a relevance score (0–1).
    Use this before creating a ticket to check whether a direct answer exists.

    Args:
        query:    The customer's question or topic to look up.
        category: Optional filter — one of: billing, returns, technical, general.
        top_k:    Maximum number of results to return (default 3).
    """
    return await kb.search(query, category, top_k)


async def search_agent_guidelines(query: str, category: str = "") -> list[dict]:
    """Search agent handling guidelines for the current customer situation.

    Call this when you need to know what information to collect from the customer
    before acting. Returns instructions written for the agent.

    Args:
        query:    Description of the current customer situation.
        category: Optional filter — billing, returns, technical, general.
    """
    return await kb.search_guideline(query, category, top_k=3)
