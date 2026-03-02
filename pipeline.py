"""
pipeline.py - End-to-end pipeline: email_stream → classifier → router → workflow agent.

Usage:
    python pipeline.py
    python pipeline.py --limit 3 --language en
"""

import argparse
import anthropic
from dotenv import load_dotenv

from email_stream import email_stream
from classifier_agent import classify
from router_agent import route

load_dotenv()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--language", type=str, default="en")
    parser.add_argument("--shuffle", action="store_true", default=True)
    args = parser.parse_args()

    client = anthropic.Anthropic()

    print(f"Pipeline starting — {args.limit} email(s), language={args.language}\n")
    print("=" * 70)

    for email in email_stream(language=args.language, limit=args.limit, shuffle=args.shuffle):
        subject = email.get("subject") or "(no subject)"
        print(f"\n EMAIL: {subject[:65]}")
        print("-" * 70)

        # Step 1: Classify
        classification = classify(client, email)
        print(f"  [classifier]  queue={classification['queue']}  "
              f"priority={classification['priority']}  type={classification['type']}")
        print(f"                reason: {classification['reason']}")

        # Step 2: Route + run workflow
        print(f"  [router]      → {classification['queue']}")
        result = route(classification, email)

        # Step 3: Print result
        print(f"  [workflow]    skill={result.skill_used}  action={result.action}  "
              f"escalated={result.escalated}")
        print(f"  [ticket]      {result.ticket_id or '(none)'}")

        if result.tool_calls:
            print(f"  [tools used]  {', '.join(c['tool'] for c in result.tool_calls)}")

        if result.reply_drafted:
            preview = result.reply_drafted.replace("\n", " ")[:200]
            print(f"  [reply]       {preview}...")

        print("=" * 70)


if __name__ == "__main__":
    main()
