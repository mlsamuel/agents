"""
eval_agent.py - LLM-as-judge evaluation of workflow agent replies.

Runs N emails through the pipeline, then uses Haiku to score each
generated reply against the ground-truth `answer` column on three
dimensions (action, completeness, tone) scored 1–5.

Usage:
    python eval_agent.py               # 3 emails
    python eval_agent.py --limit 5
    python eval_agent.py --limit 5 --language en
"""

import argparse
import json
import anthropic
from dotenv import load_dotenv

from email_stream import email_stream
from classifier_agent import classify
from orchestrator_agent import orchestrate

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


def judge(client: anthropic.Anthropic, email: dict, ground_truth: str, generated: str) -> dict:
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:600]
    gt = ground_truth[:600]
    gen = generated[:600]

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
    parser.add_argument("--language", type=str, default="en")
    args = parser.parse_args()

    client = anthropic.Anthropic()
    scores = []

    print(f"Eval — {args.limit} email(s), language={args.language}\n")
    print("=" * 70)

    for i, email in enumerate(email_stream(language=args.language, limit=args.limit, shuffle=True), 1):
        ground_truth = email.get("answer") or ""
        if not ground_truth:
            print(f"[{i}] skipped — no ground truth answer\n")
            continue

        subject = email.get("subject") or "(no subject)"
        print(f"[{i}] {subject[:65]}")

        try:
            classification = classify(client, email)
            print(f"     queue={classification['queue']}  type={classification['type']}  priority={classification['priority']}")

            result = orchestrate(classification, email)
            generated = result.final_reply or ""

            if not generated:
                print("     [skipped — no reply generated]\n")
                continue

            score = judge(client, email, ground_truth, generated)
            scores.append(score)

            avg = (score["action"] + score["completeness"] + score["tone"]) / 3
            print(f"     action={score['action']}/5  completeness={score['completeness']}/5  tone={score['tone']}/5  avg={avg:.1f}")
            print(f"     comment: {score['comment']}")

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


if __name__ == "__main__":
    main()
