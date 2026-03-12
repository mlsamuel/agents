"""
evaluator.py - LLM-as-judge evaluation using a Foundry agent.

Public API:
    judge(client, email, ground_truth, generated) -> dict
        Returns: {action: 1-5, completeness: 1-5, tone: 1-5, comment: str, avg: float}

    init_output(path)          -> None
    append_section(section, path) -> None
"""

import json
import os
from datetime import datetime
from pathlib import Path

from azure.ai.agents import AgentsClient

MODEL = os.environ.get("FAST_MODEL", os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o-mini"))

JUDGE_SYSTEM = """You are an evaluation assistant for a customer support AI.
You will be given:
  - An email (subject + body)
  - A ground-truth human reply (what a human agent actually sent)
  - A generated reply (what the AI agent produced)

Score the generated reply on three dimensions, each 1–5:
  action      - Did the agent take the right action (refund, escalate, ticket, etc.)?
                5=correct action, 1=wrong or missing action
  completeness - Did it address the customer's core concern with key details?
                5=fully addressed, 1=missed the point
  tone        - Was the tone appropriate (warm, clear, professional)?
                5=excellent, 1=cold/confusing/inappropriate

Return only valid JSON with keys: action, completeness, tone, comment
comment should be one short sentence about the biggest gap (or "none" if all good)."""


def judge(client: AgentsClient, email: dict, ground_truth: str, generated: str) -> dict:
    """Score the generated reply against the ground truth. Returns scores dict."""
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:800]
    gt = ground_truth[:600]
    gen = generated[:2000]

    user_msg = (
        f"EMAIL\nSubject: {subject}\nBody: {body}\n\n"
        f"GROUND TRUTH REPLY\n{gt}\n\n"
        f"GENERATED REPLY\n{gen}"
    )

    agent = client.agents.create_agent(
        model=MODEL,
        name="eval-judge",
        instructions=JUDGE_SYSTEM,
    )
    thread = client.agents.threads.create()
    try:
        client.agents.messages.create(thread_id=thread.id, role="user", content=user_msg)
        run = client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        if run.status != "completed":
            raise RuntimeError(f"Judge run failed: {run.status}")

        raw = ""
        for msg in client.agents.messages.list(thread_id=thread.id):
            if msg.role == "assistant":
                for part in msg.content:
                    if hasattr(part, "text"):
                        raw = part.text.value.strip()
                        break
                break
    finally:
        client.agents.threads.delete(thread.id)
        client.agents.delete_agent(agent.id)

    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    scores = json.loads(raw)
    scores["avg"] = (scores["action"] + scores["completeness"] + scores["tone"]) / 3
    return scores


# ── Output helpers ────────────────────────────────────────────────────────────

def _section_lines(s: dict) -> list[str]:
    score = s["score"]
    return [
        "---",
        f"## [{s['index']}] {s['subject']}",
        f"**Queue:** {s['queue']} | **Type:** {s['type']} | **Priority:** {s['priority']}",
        f"**Skills:** {s['skills']}  **Tools:** {s['tools']}",
        f"**Scores:** action={score['action']}/5  completeness={score['completeness']}/5  "
        f"tone={score['tone']}/5  avg={s['avg']:.1f}",
        f"**Comment:** {score['comment']}",
        "",
        "### Email",
        "```",
        f"Subject: {s['subject']}",
        "",
        s["body"],
        "```",
        "",
        "### Ground truth",
        "```",
        s["ground_truth"],
        "```",
        "",
        "### Generated",
        "```",
        s["generated"],
        "```",
        "",
    ]


def init_output(path: str = "eval_output.md") -> None:
    header = f"# Eval output — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    Path(path).write_text(header, encoding="utf-8")


def append_section(section: dict, path: str = "eval_output.md") -> None:
    lines = _section_lines(section)
    with Path(path).open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
