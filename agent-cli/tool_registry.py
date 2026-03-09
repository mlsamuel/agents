"""
tool_registry.py — single source of truth for tool→CLI routing and Anthropic schemas.

To add a new tool:
  1. Add one dict to TOOLS below.
  2. Add the click command in cli.py.
"""

TOOLS: list[dict] = [
    {
        "name": "lookup_customer",
        "namespace": "crm",
        "cli_command": "lookup-customer",
        "params": {"keyword": "keyword"},
        "description": "Look up a customer record by name or subject keyword. Returns customer profile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Name or keyword to search for"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_ticket_history",
        "namespace": "crm",
        "cli_command": "ticket-history",
        "params": {"customer_id": "customer-id"},
        "description": "Retrieve the last 3 support tickets for a customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Customer ID (e.g. CUST-12345)"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "create_ticket",
        "namespace": "tickets",
        "cli_command": "create",
        "params": {
            "subject": "subject", "body": "body", "queue": "queue",
            "priority": "priority", "ticket_type": "type",
        },
        "description": "Create a new support ticket in the system. Returns the new ticket ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject":     {"type": "string"},
                "body":        {"type": "string"},
                "queue":       {"type": "string"},
                "priority":    {"type": "string"},
                "ticket_type": {"type": "string"},
            },
            "required": ["subject", "body", "queue", "priority", "ticket_type"],
        },
    },
    {
        "name": "check_order_status",
        "namespace": "orders",
        "cli_command": "check-status",
        "params": {"order_ref": "order-ref"},
        "description": "Look up the status of an order by reference number or keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_ref": {"type": "string", "description": "Order reference or keyword"},
            },
            "required": ["order_ref"],
        },
    },
    {
        "name": "process_refund",
        "namespace": "orders",
        "cli_command": "process-refund",
        "params": {"order_ref": "order-ref", "reason": "reason"},
        "description": "Initiate a refund for an order. Returns refund confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_ref": {"type": "string"},
                "reason":    {"type": "string"},
            },
            "required": ["order_ref", "reason"],
        },
    },
    {
        "name": "escalate_to_human",
        "namespace": "comms",
        "cli_command": "escalate",
        "params": {"ticket_id": "ticket-id", "reason": "reason"},
        "description": "Escalate a ticket to a human agent. Use when issue is complex or customer is frustrated.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "reason":    {"type": "string"},
            },
            "required": ["ticket_id", "reason"],
        },
    },
    {
        "name": "send_reply",
        "namespace": "comms",
        "cli_command": "send-reply",
        "params": {"message": "message", "ticket_id": "ticket-id"},
        "description": (
            "Send a reply message to the customer. "
            "ticket_id is optional — omit when no ticket has been created yet "
            "(e.g. clarification replies)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message":   {"type": "string"},
                "ticket_id": {"type": "string", "description": "Optional ticket ID"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "search_knowledge_base",
        "namespace": "kb",
        "cli_command": "search",
        "params": {"query": "query", "category": "category", "top_k": "top-k"},
        "description": (
            "Search the support knowledge base for policy answers relevant to a query. "
            "Returns up to top_k entries with answer text and a relevance score (0–1). "
            "Use this before creating a ticket to check whether a direct answer exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string", "description": "The customer's question or topic to look up"},
                "category": {"type": "string", "description": "Optional filter: billing, returns, technical, general"},
                "top_k":    {"type": "integer", "description": "Maximum number of results (default 3)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_agent_guidelines",
        "namespace": "kb",
        "cli_command": "guidelines",
        "params": {"query": "query", "category": "category"},
        "description": (
            "Search agent handling guidelines for the current customer situation. "
            "Call this when you need to know what information to collect from the customer "
            "before acting. Returns instructions written for the agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string", "description": "Description of the current customer situation"},
                "category": {"type": "string", "description": "Optional filter: billing, returns, technical, general"},
            },
            "required": ["query"],
        },
    },
]

# Anthropic API tool schemas (strips routing keys)
SCHEMAS: list[dict] = [
    {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
    for t in TOOLS
]

# tool_name → (namespace, cli_command, params)
BY_NAME: dict[str, tuple[str, str, dict]] = {
    t["name"]: (t["namespace"], t["cli_command"], t["params"]) for t in TOOLS
}

# (namespace, function_name) → (namespace, cli_command, params)
BY_NS_FN: dict[tuple[str, str], tuple[str, str, dict]] = {
    (t["namespace"], t["name"]): (t["namespace"], t["cli_command"], t["params"]) for t in TOOLS
}

# namespace → [function_name, ...]  (passed to Docker sandbox via NAMESPACE_METHODS env var)
BY_NAMESPACE: dict[str, list[str]] = {}
for _t in TOOLS:
    BY_NAMESPACE.setdefault(_t["namespace"], []).append(_t["name"])
