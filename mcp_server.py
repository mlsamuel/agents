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

Run standalone (for testing):
  conda run -n base python mcp_server.py
"""

import random
import string
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
