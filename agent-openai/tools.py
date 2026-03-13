"""
tools.py - In-process tool implementations for specialist agents.

These Python functions are registered as OpenAI function tools.
The dispatch loop in agent_utils.py calls them when the model requests a tool.

Tools:
    lookup_customer, get_ticket_history, create_ticket,
    check_order_status, process_refund, escalate_to_human, send_reply

TOOL_DEFINITIONS — OpenAI function tool schemas for Assistants API.
ALL_TOOLS        — {name: callable} registry used by the dispatch loop.
"""

import json
import random
import string
from datetime import datetime, timedelta

# ── helpers ──────────────────────────────────────────────────────────────────

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


# ── tools ────────────────────────────────────────────────────────────────────

def lookup_customer(keyword: str) -> str:
    """Look up a customer record by name or subject keyword. Returns customer profile as JSON.

    Args:
        keyword: A name, subject line word, or account keyword to match the customer.
    """
    first, last = _customer_from_keyword(keyword)
    rng = random.Random(hash(keyword.lower().strip()))
    result = {
        "customer_id": "CUST-" + "".join(rng.choices(string.digits, k=5)),
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}@example.com",
        "account_tier": rng.choice(["standard", "premium", "enterprise"]),
        "since": _days_ago(rng.randint(100, 1000)),
        "keyword_matched": keyword,
    }
    return json.dumps(result)


def get_ticket_history(customer_id: str) -> str:
    """Retrieve the last 3 support tickets for a customer. Returns list of tickets as JSON.

    Args:
        customer_id: The customer's ID (from lookup_customer).
    """
    queues = ["Technical Support", "Billing and Payments", "Returns and Exchanges"]
    statuses = ["resolved", "closed", "open"]
    tickets = [
        {
            "ticket_id": _ticket_id(),
            "subject": f"Issue #{i+1}",
            "queue": random.choice(queues),
            "status": random.choice(statuses),
            "created": _days_ago(random.randint(5, 90)),
        }
        for i in range(3)
    ]
    return json.dumps(tickets)


def create_ticket(
    subject: str,
    body: str,
    queue: str,
    priority: str,
    ticket_type: str,
) -> str:
    """Create a new support ticket. Returns the new ticket details including ticket_id as JSON.

    Args:
        subject: Short description of the issue.
        body: Full details of the issue.
        queue: Support queue (e.g. "Billing and Payments", "Technical Support").
        priority: One of: critical, high, medium, low, very_low.
        ticket_type: One of: Incident, Problem, Request, Change, Question, Complaint.
    """
    tid = _ticket_id()
    result = {
        "ticket_id": tid,
        "subject": subject,
        "queue": queue,
        "priority": priority,
        "type": ticket_type,
        "status": "open",
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "url": f"https://support.example.com/tickets/{tid}",
    }
    return json.dumps(result)


def check_order_status(order_ref: str) -> str:
    """Look up the status of an order by reference number or keyword. Returns order details as JSON.

    Args:
        order_ref: Order ID (e.g. ORD-12345678) or descriptive keyword from the email.
    """
    statuses = ["delivered", "shipped", "processing", "cancelled", "return_initiated"]
    status = random.choice(statuses)
    result = {
        "order_id": order_ref if order_ref.startswith("ORD-") else _order_id(),
        "status": status,
        "items": [{"sku": "PROD-001", "qty": 1, "price": 49.99}],
        "total": 49.99,
        "ordered": _days_ago(random.randint(2, 30)),
        "estimated_delivery": _days_ago(-random.randint(1, 5)) if status == "shipped" else None,
    }
    return json.dumps(result)


def process_refund(order_ref: str, reason: str) -> str:
    """Initiate a refund for an order. Returns refund confirmation as JSON.

    Args:
        order_ref: Order ID or reference from the customer's email.
        reason: Brief reason for the refund.
    """
    result = {
        "refund_id": "REF-" + "".join(random.choices(string.digits, k=7)),
        "order_ref": order_ref,
        "amount": 49.99,
        "reason": reason,
        "status": "approved",
        "expected_days": 5,
        "message": "Refund approved and will appear in 3-5 business days.",
    }
    return json.dumps(result)


def escalate_to_human(ticket_id: str, reason: str) -> str:
    """Escalate a ticket to a human agent. Use when issue is complex, critical, or customer is frustrated.

    Args:
        ticket_id: The ticket ID to escalate.
        reason: Why this needs human attention.
    """
    result = {
        "ticket_id": ticket_id,
        "escalated": True,
        "assigned_to": "Senior Support Team",
        "reason": reason,
        "eta": "2 business hours",
        "message": "Ticket escalated. A specialist will contact the customer shortly.",
    }
    return json.dumps(result)


def send_reply(message: str, ticket_id: str = "") -> str:
    """Send a reply message to the customer. Call this as the final step after all actions are complete.

    Args:
        message: The full customer-facing email reply (plain prose, no markdown).
        ticket_id: Optional ticket ID to associate with the reply.
    """
    result = {
        "ticket_id": ticket_id,
        "sent": True,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preview": message[:120] + ("..." if len(message) > 120 else ""),
    }
    return json.dumps(result)


# ── tool registry ─────────────────────────────────────────────────────────────

ALL_TOOLS: dict[str, object] = {
    "lookup_customer":    lookup_customer,
    "get_ticket_history": get_ticket_history,
    "create_ticket":      create_ticket,
    "check_order_status": check_order_status,
    "process_refund":     process_refund,
    "escalate_to_human":  escalate_to_human,
    "send_reply":         send_reply,
}

# OpenAI function tool schemas — passed to assistants.create(tools=[...])
TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_customer",
            "description": "Look up a customer record by name or subject keyword. Returns customer profile as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "A name, subject line word, or account keyword to match the customer.",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket_history",
            "description": "Retrieve the last 3 support tickets for a customer. Returns list of tickets as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "string",
                        "description": "The customer's ID (from lookup_customer).",
                    }
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create a new support ticket. Returns the new ticket details including ticket_id as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject":     {"type": "string", "description": "Short description of the issue."},
                    "body":        {"type": "string", "description": "Full details of the issue."},
                    "queue":       {"type": "string", "description": 'Support queue, e.g. "Billing and Payments", "Technical Support".'},
                    "priority":    {"type": "string", "enum": ["critical", "high", "medium", "low", "very_low"]},
                    "ticket_type": {"type": "string", "enum": ["Incident", "Problem", "Request", "Change", "Question", "Complaint"]},
                },
                "required": ["subject", "body", "queue", "priority", "ticket_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_order_status",
            "description": "Look up the status of an order by reference number or keyword. Returns order details as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_ref": {
                        "type": "string",
                        "description": "Order ID (e.g. ORD-12345678) or descriptive keyword from the email.",
                    }
                },
                "required": ["order_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_refund",
            "description": "Initiate a refund for an order. Returns refund confirmation as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_ref": {"type": "string", "description": "Order ID or reference from the customer's email."},
                    "reason":    {"type": "string", "description": "Brief reason for the refund."},
                },
                "required": ["order_ref", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Escalate a ticket to a human agent. Use when issue is complex, critical, or customer is frustrated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "The ticket ID to escalate."},
                    "reason":    {"type": "string", "description": "Why this needs human attention."},
                },
                "required": ["ticket_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_reply",
            "description": "Send a reply message to the customer. Call this as the final step after all actions are complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message":   {"type": "string", "description": "The full customer-facing email reply (plain prose, no markdown)."},
                    "ticket_id": {"type": "string", "description": "Optional ticket ID to associate with the reply."},
                },
                "required": ["message"],
            },
        },
    },
]
