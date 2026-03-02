"""
router_agent.py - Routes a classified email to the correct WorkflowAgent.

Queue → agent_key mapping. Multiple queues can share an agent (e.g. IT Support
and Technical Support both route to the technical_support skill folder).
"""

from workflow_agent import WorkflowAgent, WorkflowResult

QUEUE_MAP: dict[str, str] = {
    "Technical Support":              "technical_support",
    "IT Support":                     "technical_support",
    "Product Support":                "technical_support",
    "Service Outages and Maintenance":"technical_support",
    "Billing and Payments":           "billing",
    "Returns and Exchanges":          "returns",
    "Customer Service":               "general",
    "Sales and Pre-Sales":            "general",
    "General Inquiry":                "general",
    "Human Resources":                "general",
}


def route(classification: dict, email: dict) -> WorkflowResult:
    """Dispatch a classified email to the appropriate workflow agent."""
    queue = classification.get("queue", "")
    agent_key = QUEUE_MAP.get(queue, "general")
    agent = WorkflowAgent(agent_key)
    return agent.run(email, classification)
