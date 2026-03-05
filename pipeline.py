"""
pipeline.py - End-to-end pipeline:
  email_stream → classifier → orchestrator → workflow agents (parallel) → merged reply

Usage:
    python pipeline.py
    python pipeline.py --limit 3 --language en
"""

import argparse
import asyncio
from dotenv import load_dotenv
from client import Client

from email_stream import email_stream
from classifier_agent import classify
from orchestrator_agent import orchestrate
from input_screener import screen_email
from email_sanitizer import sanitize
import kb

load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--language", type=str, default="en")
    parser.add_argument("--shuffle", action="store_true", default=False)
    parser.add_argument("--screen", default=True, action=argparse.BooleanOptionalAction,
                        help="Run input screener (Haiku injection detector). Default: on.")
    args = parser.parse_args()

    asyncio.run(kb.get_pool())

    client = Client()

    print(f"Pipeline starting — {args.limit} email(s), language={args.language}\n")
    print("=" * 70)

    for email in email_stream(language=args.language, limit=args.limit, shuffle=args.shuffle):
        subject = email.get("subject") or "(no subject)"
        print(f"\n EMAIL: {subject[:65]}")
        print("-" * 70)

        # Step 1: Screen for injection (on raw email, before any sanitization)
        if args.screen:
            screen = screen_email(client, email)
            if not screen.safe:
                print(f"  [screener]    QUARANTINED (score={screen.risk_score}/10) — {screen.reason}")
                print("=" * 70)
                continue
            if screen.risk_score >= 3:
                print(f"  [screener]    warning score={screen.risk_score}/10 — {screen.reason}")

        # Step 2: Sanitize (pattern strip)
        email, warnings = sanitize(email)
        if warnings:
            print(f"  [sanitizer]   stripped {len(warnings)} pattern(s)")

        # Step 3: Classify
        classification = classify(client, email)
        print(f"  [classifier]  queue={classification['queue']}  "
              f"priority={classification['priority']}  type={classification['type']}")
        print(f"                reason: {classification['reason']}")

        # Step 4: Orchestrate (decompose → fan out → merge)
        result = orchestrate(classification, email)

        multi = len(result.agents_used) > 1
        print(f"  [orchestrator] agents={result.agents_used}  "
              f"{'MULTI-AGENT  ' if multi else ''}"
              f"action={result.action}  escalated={result.escalated}")

        for sub in result.results:
            print(f"    ↳ [{sub.skill_used}]  ticket={sub.ticket_id or '(none)'}  "
                  f"tools={[c['tool'] for c in sub.tool_calls]}")

        if result.ticket_ids:
            print(f"  [tickets]     {', '.join(result.ticket_ids)}")

        if result.final_reply:
            preview = result.final_reply.replace("\n", " ")[:220]
            print(f"  [final reply] {preview}...")

        print("=" * 70)


if __name__ == "__main__":
    main()
