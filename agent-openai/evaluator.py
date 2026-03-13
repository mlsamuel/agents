"""
evaluator.py - LLM-as-judge evaluation using Chat Completions.

Uses Chat Completions with response_format=json_object for guaranteed JSON output.

Public API:
    judge(client, email, ground_truth, generated) -> dict
        Returns: {action: 1-5, completeness: 1-5, tone: 1-5, comment: str, avg: float}

    init_output(path)             -> None
    append_section(section, path) -> None
"""

import json
import os
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agent_utils import run_simple
from logger import get_logger

log = get_logger(__name__)

MODEL = os.environ.get("FAST_MODEL", "gpt-4o-mini")

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

Return JSON with keys: action, completeness, tone, comment
comment should be one short sentence about the biggest gap (or "none" if all good)."""


def judge(client: OpenAI, email: dict, ground_truth: str, generated: str) -> dict:
    """Score the generated reply against the ground truth. Returns scores dict."""
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:800]

    user_msg = (
        f"EMAIL\nSubject: {subject}\nBody: {body}\n\n"
        f"GROUND TRUTH REPLY\n{ground_truth[:600]}\n\n"
        f"GENERATED REPLY\n{generated[:2000]}"
    )

    raw = run_simple(
        client,
        system=JUDGE_SYSTEM,
        user_msg=user_msg,
        model=MODEL,
        response_format={"type": "json_object"},
    )

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
        f"**Skills:** {s['skills']}  **Tools:** {s['tools']}"
        + (f"  **KB:** {', '.join(s['files_searched'])}" if s.get("files_searched") else ""),
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
