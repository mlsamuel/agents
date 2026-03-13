"""
generate_guidelines.py - Extract behavioural guidelines from emails.csv.

The existing agent_guidelines.json has only 6 hand-written entries. This script
samples English emails per domain, sends them in batches to GPT-4o, and asks it
to infer agent behavioural patterns from the (email, answer) pairs. The result
is merged into data/agent_guidelines.json, expanding the guideline set for SFT.

Run once before generate_dataset.py:
    python sft/generate_guidelines.py

Flags:
    --per-domain    Number of email pairs to sample per domain (default: 40)
    --guidelines    Target guideline count per domain to extract (default: 6)
    --dry-run       Print proposed guidelines without writing to disk
"""

import argparse
import csv
import json
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Add parent dir so we can import store
sys.path.insert(0, str(Path(__file__).parent.parent))
import store

load_dotenv(Path(__file__).parent.parent / ".env")

DATA_DIR    = Path(__file__).parent.parent / "data"
EMAILS_CSV  = DATA_DIR / "emails.csv"

FAST_MODEL = os.environ.get("FAST_MODEL", "gpt-4o-mini")
STRONG_MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")

# Map internal agent keys → CSV queue values for sampling
_DOMAIN_QUEUES = {
    "technical_support": ["Technical Support", "IT Support", "Product Support",
                          "Service Outages and Maintenance"],
    "billing":           ["Billing and Payments"],
    "returns":           ["Returns and Exchanges"],
    "general":           ["General Inquiry", "Customer Service", "Sales and Pre-Sales",
                          "Human Resources"],
}

EXTRACT_SYSTEM = """\
You are an expert at analysing customer support email exchanges to extract agent behavioural guidelines.

You will receive a batch of (email subject, email body, agent reply) triples from one support domain.
Analyse the patterns in how the agent responds and extract DISTINCT behavioural guidelines.

Focus on:
- When the agent asks clarifying questions before acting
- When the agent escalates vs. resolves directly
- When the agent requests specific information (model numbers, account IDs, etc.)
- Tonal patterns for different email types (complaints vs. requests vs. incidents)
- Decision rules the agent applies (e.g. "if outage affects multiple users, escalate")

Output a JSON array of guideline objects. Each object must have:
  - category:    one of [technical_support, billing, returns, general]
  - topic:       short descriptive title (5–8 words)
  - trigger:     the condition or email pattern that triggers this behaviour
  - instruction: what the agent should do (specific, actionable)
  - keywords:    3–6 relevant keywords

Rules:
- Extract only patterns supported by MULTIPLE examples in the batch
- Do NOT duplicate guidelines already in the existing list (provided below)
- Each guideline must be specific and actionable, not generic ("be professional")
- Aim for {n} guidelines. Return fewer if patterns don't support more.
- Respond with a JSON array only, no markdown wrapper."""


def _load_emails_by_domain(per_domain: int, seed: int = 42) -> dict[str, list[dict]]:
    """Sample emails from emails.csv, stratified by domain, English only."""
    rng = random.Random(seed)
    domain_rows: dict[str, list[dict]] = {k: [] for k in _DOMAIN_QUEUES}

    with open(EMAILS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("language") != "en":
                continue
            if not row.get("answer"):
                continue
            for domain, queues in _DOMAIN_QUEUES.items():
                if row.get("queue") in queues:
                    domain_rows[domain].append(row)
                    break

    sampled = {}
    for domain, rows in domain_rows.items():
        sampled[domain] = rng.sample(rows, min(per_domain, len(rows)))
    return sampled


def _format_batch(rows: list[dict]) -> str:
    parts = []
    for i, row in enumerate(rows, 1):
        subject = row.get("subject", "(no subject)")[:100]
        body    = (row.get("body") or "")[:400]
        answer  = (row.get("answer") or "")[:400]
        parts.append(
            f"--- Example {i} ---\n"
            f"Subject: {subject}\n"
            f"Body: {body}\n"
            f"Agent reply: {answer}"
        )
    return "\n\n".join(parts)


def _extract_guidelines(
    client: OpenAI,
    domain: str,
    rows: list[dict],
    existing: list[dict],
    n_target: int,
) -> list[dict]:
    """Ask the model to extract guidelines from a batch of email examples."""
    existing_topics = "\n".join(
        f"- [{g['category']}] {g['topic']}" for g in existing
    )
    system = EXTRACT_SYSTEM.format(n=n_target).replace(
        "Respond with a JSON array only, no markdown wrapper.",
        f"EXISTING GUIDELINES (do not duplicate):\n{existing_topics}\n\nRespond with a JSON array only, no markdown wrapper."
    )

    batch_text = _format_batch(rows)
    user_msg = (
        f"Domain: {domain}\n\n"
        f"Email examples:\n\n{batch_text}\n\n"
        f"Extract up to {n_target} behavioural guidelines from the above."
    )

    response = client.chat.completions.create(
        model=STRONG_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "[]"

    # Model may return {"guidelines": [...]} or just [...]
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = parsed.get("guidelines", parsed.get("items", []))
    if not isinstance(parsed, list):
        return []

    # Ensure required fields and set category
    result = []
    for g in parsed:
        if not g.get("topic") or not g.get("instruction"):
            continue
        g.setdefault("category", domain)
        g.setdefault("trigger", "")
        g.setdefault("keywords", [])
        result.append(g)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract agent guidelines from emails.csv")
    parser.add_argument("--per-domain",   type=int, default=40)
    parser.add_argument("--guidelines",   type=int, default=6,
                        help="Target number of new guidelines to extract per domain")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Print proposed guidelines without writing to disk")
    parser.add_argument("--seed",         type=int, default=42)
    args = parser.parse_args()

    client = OpenAI()
    domain_rows = _load_emails_by_domain(args.per_domain, seed=args.seed)
    existing = store.load_guidelines()

    print(f"Existing guidelines: {len(existing)}")
    print(f"Sampling {args.per_domain} emails per domain, targeting {args.guidelines} new guidelines each\n")

    all_new: list[dict] = []
    for domain, rows in domain_rows.items():
        print(f"  [{domain}] {len(rows)} examples → extracting guidelines…")
        new = _extract_guidelines(client, domain, rows, existing + all_new, args.guidelines)
        print(f"             {len(new)} extracted")
        for g in new:
            print(f"             • {g['topic']}")
        all_new.extend(new)

    print(f"\nTotal new guidelines: {len(all_new)}")

    if args.dry_run:
        print("\nDRY RUN — not writing to disk")
        print(json.dumps(all_new, indent=2, ensure_ascii=False))
        return

    # Merge into existing guidelines (add_guideline handles dedup by topic)
    added = 0
    for g in all_new:
        before = len(store.load_guidelines())
        store.add_guideline(g)
        after = len(store.load_guidelines())
        if after > before:
            added += 1

    total = len(store.load_guidelines())
    print(f"Added {added} new guidelines. Total: {total}")
    print(f"Written to {DATA_DIR / 'agent_guidelines.json'}")


if __name__ == "__main__":
    main()
