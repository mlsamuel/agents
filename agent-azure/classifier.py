"""
classifier.py - Classifies incoming emails using a Foundry agent.

Public API:
    classify(client, email) -> dict
        Returns: {queue, priority, type, reason, subject}
"""

import json
import os

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import AgentsResponseFormat

from agent_utils import run_with_retry
from logger import get_logger

log = get_logger(__name__)

MODEL = os.environ.get("FAST_MODEL", os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o-mini"))

SYSTEM_PROMPT = """You are an email classifier for a customer support system.
Given an email subject and body, return a JSON object with these fields:

  - queue: the single best-matching support queue. Use one of these specialist queues when
    the email clearly fits; otherwise use "General Inquiry":
      Technical Support, IT Support, Product Support, Service Outages and Maintenance,
      Billing and Payments, Returns and Exchanges, Customer Service, Sales and Pre-Sales,
      Human Resources, General Inquiry

  - priority: one of [critical, high, medium, low, very_low]
  - type:     one of [Incident, Problem, Request, Change, Question, Complaint]
  - reason:   one short sentence explaining your classification

If the email has no subject line, rely entirely on the body for classification."""

# Map classifier queues to internal agent keys
_QUEUE_TO_AGENT: dict[str, str] = {
    "Technical Support":              "technical_support",
    "IT Support":                     "technical_support",
    "Product Support":                "technical_support",
    "Service Outages and Maintenance":"technical_support",
    "Billing and Payments":           "billing",
    "Returns and Exchanges":          "returns",
    "Customer Service":               "general",
    "Sales and Pre-Sales":            "general",
    "Human Resources":                "general",
    "General Inquiry":                "general",
}


def classify(client: AgentsClient, email: dict) -> dict:
    """Classify an email and return classification dict including agent_key."""
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:1500]

    agent = client.create_agent(
        model=MODEL,
        name="email-classifier",
        instructions=SYSTEM_PROMPT,
        response_format=AgentsResponseFormat(type="json_object"),
    )
    thread = client.threads.create()
    try:
        client.messages.create(
            thread_id=thread.id,
            role="user",
            content=f"Subject: {subject}\n\nBody:\n{body}",
        )
        run = run_with_retry(client, thread.id, agent.id)
        if run.status != "completed":
            raise RuntimeError(f"Classifier run failed: {run.status}")

        messages = client.messages.list(thread_id=thread.id)
        raw = ""
        for msg in messages:
            if msg.role == "assistant":
                for part in msg.content:
                    if hasattr(part, "text"):
                        raw = part.text.value.strip()
                        break
                break
    finally:
        client.threads.delete(thread.id)
        client.delete_agent(agent.id)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Classifier returned invalid JSON: {exc!s} — raw={raw!r}") from exc
    result["subject"] = subject
    result["agent_key"] = _QUEUE_TO_AGENT.get(result.get("queue", ""), "general")
    return result
