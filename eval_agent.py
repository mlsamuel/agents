"""
eval_agent.py - LLM-as-judge evaluation of workflow agent replies.

Runs N emails through the pipeline, then uses Haiku to score each
generated reply against the ground-truth `answer` column on three
dimensions (action, completeness, tone) scored 1–5.

Usage:
    python eval_agent.py               # 3 emails, saves side-by-side to eval_output.md
    python eval_agent.py --limit 5
    python eval_agent.py --offset 2 --limit 1   # run only the 3rd email
    python eval_agent.py --no-save     # skip writing the output file
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from client import Client
from email_stream import email_stream
from classifier_agent import classify
from orchestrator_agent import orchestrate
from input_screener import screen_email
import kb

load_dotenv()

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N emails")
    parser.add_argument("--language", type=str, default="en")
    parser.add_argument("--save", default=True, action=argparse.BooleanOptionalAction,
                        help="Write side-by-side replies to eval_output.md (default: true)")
    parser.add_argument("--internal-summary", default=False, action=argparse.BooleanOptionalAction,
                        help="Include ### Internal summary sections in eval_output.md (default: false)")
    parser.add_argument("--screen", default=True, action=argparse.BooleanOptionalAction,
                        help="Run input screener before each email (default: true)")
    args = parser.parse_args()

    asyncio.run(kb.get_pool())

    client = Client()
    scores = []
    output_sections = []

    print(f"Eval — {args.limit} email(s), offset={args.offset}, language={args.language}\n")
    print("=" * 70)

    for i, email in enumerate(email_stream(language=args.language, limit=args.limit, offset=args.offset), args.offset + 1):
        ground_truth = email.get("answer") or ""
        if not ground_truth:
            print(f"[{i}] skipped — no ground truth answer\n")
            continue

        subject = email.get("subject") or "(no subject)"
        print(f"[{i}] {subject[:65]}")

        if args.screen:
            screen = screen_email(client, email)
            if not screen.safe:
                print(f"     [screener] QUARANTINED (score={screen.risk_score}/10) — {screen.reason}\n")
                continue
            if screen.risk_score >= 3:
                print(f"     [screener] warning score={screen.risk_score}/10 — {screen.reason}")

        try:
            classification = classify(client, email)
            print(f"     queue={classification['queue']}  type={classification['type']}  priority={classification['priority']}")

            result = orchestrate(classification, email)
            generated = result.final_reply or ""
            internal_summary = result.results[0].internal_summary if result.results else ""

            # Show skills and tools used across all sub-agents, flagging run_code
            skills_str = ", ".join(sub.skill_used for sub in result.results)
            all_tools = [c["tool"] for sub in result.results for c in sub.tool_calls]
            tools_str = ", ".join(all_tools) if all_tools else "(none)"
            run_code_used = "run_code" in all_tools
            print(f"     skill: {skills_str}")
            print(f"     tools: {tools_str}{' ← used run_code' if run_code_used else ''}")

            if not generated:
                print("     [skipped — no reply generated]\n")
                continue

            score = judge(client, email, ground_truth, generated)
            scores.append(score)

            avg = (score["action"] + score["completeness"] + score["tone"]) / 3
            print(f"     action={score['action']}/5  completeness={score['completeness']}/5  tone={score['tone']}/5  avg={avg:.1f}")
            print(f"     comment: {score['comment']}")

            if args.save:
                output_sections.append({
                    "index": i,
                    "subject": subject,
                    "body": email.get("body") or "",
                    "queue": classification["queue"],
                    "type": classification["type"],
                    "priority": classification["priority"],
                    "skills": skills_str,
                    "tools": tools_str,
                    "ground_truth": ground_truth,
                    "generated": generated,
                    "internal_summary": internal_summary,
                    "score": score,
                    "avg": avg,
                })

        except Exception as e:
            print(f"     [error: {e}]")

        print()

    if scores:
        print("=" * 70)
        n = len(scores)
        avg_action       = sum(s["action"]       for s in scores) / n
        avg_completeness = sum(s["completeness"] for s in scores) / n
        avg_tone         = sum(s["tone"]         for s in scores) / n
        overall          = (avg_action + avg_completeness + avg_tone) / 3
        print(f"SUMMARY ({n} emails)")
        print(f"  action:       {avg_action:.1f}/5")
        print(f"  completeness: {avg_completeness:.1f}/5")
        print(f"  tone:         {avg_tone:.1f}/5")
        print(f"  overall:      {overall:.1f}/5")

    if args.save and output_sections:
        _write_output(output_sections, include_internal_summary=args.internal_summary)
        _write_json(output_sections)


def _write_json(sections: list[dict], path: str = "eval_results.json") -> None:
    """Write structured eval results for downstream processing by improve_agent.py."""
    Path(path).write_text(json.dumps(sections, indent=2), encoding="utf-8")
    print(f"Saved to {path}")


def _write_output(sections: list[dict], path: str = "eval_output.md", include_internal_summary: bool = True) -> None:
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


if __name__ == "__main__":
    main()
