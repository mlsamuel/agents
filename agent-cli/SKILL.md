# Support Agent CLI — Skill Reference

A CLI interface for the customer support backend. All commands output structured
JSON to stdout. Errors are also returned as JSON: `{"error": "..."}`.

## Prerequisites

- Python environment with dependencies installed (`pip install -r requirements.txt`)
- PostgreSQL running (`docker-compose up -d`) — required for `kb` commands only
- `.env` file at the project root with `DATABASE_URL` and `ANTHROPIC_API_KEY`

## Usage

```
python cli.py <namespace> <command> [--flags]
```

Inspect available commands:
```
python cli.py --help
python cli.py <namespace> --help
python cli.py <namespace> <command> --help
```

---

## Namespaces

### `crm` — Customer Records

#### `lookup-customer`
Find a customer by name or keyword from the email subject.

```bash
python cli.py crm lookup-customer --keyword "Jane Smith"
```

**Output:**
```json
{
  "customer_id": "CUST-83021",
  "name": "Jane Smith",
  "email": "jane.smith@example.com",
  "account_tier": "premium",
  "since": "2022-04-11",
  "keyword_matched": "Jane Smith"
}
```

#### `ticket-history`
Retrieve the last 3 support tickets for a customer.

```bash
python cli.py crm ticket-history --customer-id CUST-83021
```

**Output:**
```json
[
  {"ticket_id": "TKT-409231", "subject": "Issue #1", "queue": "Billing and Payments", "status": "resolved", "created": "2024-12-01"},
  {"ticket_id": "TKT-882341", "subject": "Issue #2", "queue": "Technical Support",    "status": "closed",   "created": "2024-10-14"},
  {"ticket_id": "TKT-119023", "subject": "Issue #3", "queue": "Returns and Exchanges", "status": "open",    "created": "2025-01-03"}
]
```

---

### `orders` — Order Management

#### `check-status`
Look up the status of an order.

```bash
python cli.py orders check-status --order-ref ORD-00123456
```

**Output:**
```json
{
  "order_id": "ORD-00123456",
  "status": "shipped",
  "items": [{"sku": "PROD-001", "qty": 1, "price": 49.99}],
  "total": 49.99,
  "ordered": "2025-02-20",
  "estimated_delivery": "2025-03-12"
}
```

#### `process-refund`
Initiate a refund for an order.

```bash
python cli.py orders process-refund --order-ref ORD-00123456 --reason "Item arrived damaged"
```

**Output:**
```json
{
  "refund_id": "REF-4921033",
  "order_ref": "ORD-00123456",
  "amount": 49.99,
  "reason": "Item arrived damaged",
  "status": "approved",
  "expected_days": 5,
  "message": "Refund approved and will appear in 3-5 business days."
}
```

---

### `tickets` — Ticket Operations

#### `create`
Open a new support ticket.

```bash
python cli.py tickets create \
  --subject "Billing charge not recognised" \
  --body "Customer reports an unexpected charge on 2025-02-15" \
  --queue "Billing and Payments" \
  --priority high \
  --type Incident
```

**Output:**
```json
{
  "ticket_id": "TKT-761204",
  "subject": "Billing charge not recognised",
  "queue": "Billing and Payments",
  "priority": "high",
  "type": "Incident",
  "status": "open",
  "created": "2025-03-09 14:22",
  "url": "https://support.example.com/tickets/TKT-761204"
}
```

---

### `comms` — Customer Communication

#### `send-reply`
Send a reply to the customer. `--ticket-id` is optional when no ticket exists yet.

```bash
python cli.py comms send-reply \
  --message "Hi Jane, I've initiated a refund of $49.99 for order ORD-00123456. It will appear in 3-5 business days." \
  --ticket-id TKT-761204
```

**Output:**
```json
{
  "ticket_id": "TKT-761204",
  "sent": true,
  "timestamp": "2025-03-09 14:23",
  "preview": "Hi Jane, I've initiated a refund of $49.99 for order ORD-00123456. It will appear in 3-5 business days."
}
```

#### `escalate`
Escalate a ticket to a human agent.

```bash
python cli.py comms escalate --ticket-id TKT-761204 --reason "Customer is requesting a supervisor"
```

**Output:**
```json
{
  "ticket_id": "TKT-761204",
  "escalated": true,
  "assigned_to": "Senior Support Team",
  "reason": "Customer is requesting a supervisor",
  "eta": "2 business hours",
  "message": "Ticket escalated. A specialist will contact the customer shortly."
}
```

---

### `kb` — Knowledge Base

> Requires a running PostgreSQL instance (`docker-compose up -d`).

#### `search`
Search the support knowledge base for policy answers.

```bash
python cli.py kb search --query "refund policy for digital products" --category billing --top-k 2
```

**Output:**
```json
[
  {
    "topic": "Digital product refunds",
    "answer": "Digital products are eligible for a full refund within 14 days of purchase if unused.",
    "score": 0.87
  },
  {
    "topic": "Refund processing time",
    "answer": "Refunds are processed within 3-5 business days after approval.",
    "score": 0.74
  }
]
```

#### `guidelines`
Search agent handling guidelines for the current situation.

```bash
python cli.py kb guidelines --query "customer disputing a charge they do not recognise"
```

**Output:**
```json
[
  {
    "topic": "Billing dispute intake",
    "instruction": "Before actioning: collect the transaction date, amount, and last 4 digits of the payment method.",
    "score": 0.91
  }
]
```

---

## Agent Integration Pattern

AI agents invoke these commands via subprocess and parse the JSON stdout:

```python
import subprocess, json, sys
from pathlib import Path

CLI = [sys.executable, str(Path(__file__).parent / "cli.py")]

def call_tool(namespace, command, **flags):
    args = CLI + [namespace, command]
    for key, val in flags.items():
        if val not in (None, ""):
            args += [f"--{key.replace('_', '-')}", str(val)]
    result = subprocess.run(args, capture_output=True, text=True)
    return json.loads(result.stdout)

# Example
customer = call_tool("crm", "lookup-customer", keyword="Jane Smith")
```

See [`workflow_agent.py`](workflow_agent.py) for the full implementation.
