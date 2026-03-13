"""
classifier.py - Classifies incoming emails using Chat Completions.

Uses Chat Completions (not Assistants) with response_format=json_object for
guaranteed JSON output — no thread lifecycle overhead for a stateless classify call.

Public API:
    classify(client, email) -> dict
        Returns: {queue, priority, type, reason, subject, agent_key}
"""

import json
import os

from openai import OpenAI

from agent_utils import run_simple
from logger import get_logger

log = get_logger(__name__)

MODEL = os.environ.get("FAST_MODEL", "gpt-4o-mini")

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

# Map classifier queues → internal agent keys used by orchestrator
_QUEUE_TO_AGENT: dict[str, str] = {
    "Technical Support":               "technical_support",
    "IT Support":                      "technical_support",
    "Product Support":                 "technical_support",
    "Service Outages and Maintenance": "technical_support",
    "Billing and Payments":            "billing",
    "Returns and Exchanges":           "returns",
    "Customer Service":                "general",
    "Sales and Pre-Sales":             "general",
    "Human Resources":                 "general",
    "General Inquiry":                 "general",
}


def classify(client: OpenAI, email: dict) -> dict:
    """Classify an email and return classification dict including agent_key."""
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:1500]

    raw = run_simple(
        client,
        system=SYSTEM_PROMPT,
        user_msg=f"Subject: {subject}\n\nBody:\n{body}",
        model=MODEL,
        response_format={"type": "json_object"},
    )

    result = json.loads(raw)
    result["subject"] = subject
    result["agent_key"] = _QUEUE_TO_AGENT.get(result.get("queue", ""), "general")
    return result
