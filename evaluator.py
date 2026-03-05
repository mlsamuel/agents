"""
evaluator.py - LLM-as-judge evaluation helpers.

Public API:
  judge(client, email, ground_truth, generated) → dict
  write_output(sections, path, include_internal_summary)
"""

import json
from datetime import datetime
from pathlib import Path
from client import Client

MODEL = "claude-haiku-4-5-20251001"

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


def judge(client: Client, email: dict, ground_truth: str, generated: str) -> dict:
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:800]
    gt = ground_truth[:600]
    gen = generated[:2000]

    msg = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"EMAIL\nSubject: {subject}\nBody: {body}\n\n"
                f"GROUND TRUTH REPLY\n{gt}\n\n"
                f"GENERATED REPLY\n{gen}"
            ),
        }],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    return json.loads(raw)


def write_output(sections: list[dict], path: str = "eval_output.md", include_internal_summary: bool = True) -> None:
    lines = [
        f"# Eval output — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"*{len(sections)} email(s)*",
        "",
    ]
    for s in sections:
        score = s["score"]
        avg = s["avg"]
        lines += [
            f"---",
            f"## [{s['index']}] {s['subject']}",
            f"**Queue:** {s['queue']} | **Type:** {s['type']} | **Priority:** {s['priority']}",
            f"**Skills:** {s['skills']}",
            f"**Tools:** {s['tools']}",
            f"**Scores:** action={score['action']}/5  completeness={score['completeness']}/5  tone={score['tone']}/5  avg={avg:.1f}",
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
            *(
                [
                    "### Internal summary",
                    "```",
                    s["internal_summary"],
                    "```",
                    "",
                ]
                if include_internal_summary and s["internal_summary"]
                else []
            ),
        ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved to {path}")
