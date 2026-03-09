"""
classifier.py - Classifies incoming emails using Claude.

Public API:
  classify(client, email) → dict
"""

import json
from client import Client

MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for classification

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

If the email has no subject line, rely entirely on the body for classification.
Respond with only valid JSON, no markdown fences."""


def classify(client: Client, email: dict) -> dict:
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:1500]  # trim very long bodies

    message = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Subject: {subject}\n\nBody:\n{body}",
            }
        ],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if the model added them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    result = json.loads(raw)
    result["subject"] = subject
    result["ground_truth_queue"] = email.get("queue")
    result["ground_truth_priority"] = email.get("priority")
    return result


