"""
generate_dataset.py - Create SFT training and evaluation datasets from emails.csv.

Produces two JSONL files in OpenAI fine-tuning format:
  data/sft/train.jsonl  — 100 examples (25 per domain)
  data/sft/eval.jsonl   — 20 examples  (5 per domain, held out, no overlap with train)

Each example:
  system:    base instructions + domain KB entries + all guidelines
  user:      email subject + body
  assistant: ground-truth answer from emails.csv

The system prompt includes:
  - KB entries for the email's domain (simulates retrieval — keeps context manageable)
  - All agent guidelines (these will be ABSENT from the fine-tuned model's inference prompt
    to prove the model learned them during training)

At inference, file_search retrieves KB from the vector store — same content, same position
in context (before the email). The fine-tuned model is tested without guidelines to verify
they were baked into weights.

Run after generate_guidelines.py:
    python sft/generate_dataset.py

Flags:
    --train-per-domain   Examples per domain in train set (default: 25)
    --eval-per-domain    Examples per domain in eval set (default: 5)
    --seed               Random seed (default: 42)
"""

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
import store
from kb_setup import build_category_markdowns, build_guidelines_markdown

load_dotenv(Path(__file__).parent.parent / ".env")

DATA_DIR   = Path(__file__).parent.parent / "data"
EMAILS_CSV = DATA_DIR / "emails.csv"
SFT_DIR    = DATA_DIR / "sft"

# Map CSV queue → internal domain key (matches classifier._QUEUE_TO_AGENT)
_QUEUE_TO_DOMAIN = {
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

# Map domain key → KB category keys (from knowledge_base.json categories)
_DOMAIN_TO_KB_CATEGORIES = {
    "technical_support": ["technical", "technical_support"],
    "billing":           ["billing"],
    "returns":           ["returns"],
    "general":           ["general", "product"],
}

BASE_SYSTEM = """\
You are a customer support specialist. Handle the incoming email professionally.
Use the knowledge base entries below to answer factual questions accurately.
Follow the agent behaviour guidelines to ensure consistent, high-quality responses.
Reply in plain prose only — no markdown, no bullet points."""


def _load_emails_by_domain(seed: int) -> dict[str, list[dict]]:
    """Load English emails with answers, grouped by domain."""
    by_domain: dict[str, list[dict]] = defaultdict(list)
    with open(EMAILS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("language") != "en":
                continue
            if not row.get("answer", "").strip():
                continue
            domain = _QUEUE_TO_DOMAIN.get(row.get("queue", ""), "")
            if not domain:
                continue
            by_domain[domain].append({
                "subject": row["subject"],
                "body":    row["body"],
                "answer":  row["answer"],
                "queue":   row["queue"],
            })

    rng = random.Random(seed)
    for domain in by_domain:
        rng.shuffle(by_domain[domain])
    return dict(by_domain)


def _build_kb_context(domain: str, category_mds: dict[str, str]) -> str:
    """Return the KB entries relevant to this domain as a markdown string."""
    parts = []
    for cat in _DOMAIN_TO_KB_CATEGORIES.get(domain, ["general"]):
        if cat in category_mds:
            parts.append(category_mds[cat])
    return "\n\n".join(parts)


def _make_example(email: dict, domain: str, kb_context: str, guidelines_md: str) -> dict:
    """Format one (email, answer) pair as an OpenAI fine-tuning message dict."""
    system_parts = [BASE_SYSTEM]
    if kb_context:
        system_parts.append(f"\n## Knowledge Base\n\n{kb_context}")
    if guidelines_md:
        system_parts.append(f"\n## Agent Behaviour Guidelines\n\n{guidelines_md}")
    system = "\n".join(system_parts)
    user   = f"Subject: {email['subject']}\n\n{email['body']}"
    return {
        "messages": [
            {"role": "system",    "content": system},
            {"role": "user",      "content": user},
            {"role": "assistant", "content": email["answer"]},
        ]
    }


def _write_jsonl(path: Path, examples: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SFT dataset from emails.csv")
    parser.add_argument("--train-per-domain", type=int, default=25)
    parser.add_argument("--eval-per-domain",  type=int, default=5)
    parser.add_argument("--seed",             type=int, default=42)
    args = parser.parse_args()

    emails_by_domain = _load_emails_by_domain(args.seed)
    category_mds     = build_category_markdowns()
    guidelines_md    = build_guidelines_markdown()

    n_guidelines = guidelines_md.count("###") if guidelines_md else 0
    print(f"KB categories: {list(category_mds.keys())}")
    print(f"Guidelines loaded: {n_guidelines}")
    print()

    train_examples: list[dict] = []
    eval_examples: list[dict] = []

    for domain, rows in emails_by_domain.items():
        n_train = args.train_per_domain
        n_eval  = args.eval_per_domain
        need    = n_train + n_eval

        if len(rows) < need:
            print(f"  WARNING [{domain}]: only {len(rows)} emails available, need {need}")
            n_train = min(n_train, len(rows))
            n_eval  = min(n_eval, len(rows) - n_train)

        kb_context = _build_kb_context(domain, category_mds)

        train_rows = rows[:n_train]
        eval_rows  = rows[n_train:n_train + n_eval]

        for row in train_rows:
            train_examples.append(_make_example(row, domain, kb_context, guidelines_md))
        for row in eval_rows:
            eval_examples.append(_make_example(row, domain, kb_context, guidelines_md))

        print(f"  [{domain}] train={len(train_rows)}  eval={len(eval_rows)}")

    # Shuffle to mix domains
    rng = random.Random(args.seed)
    rng.shuffle(train_examples)
    rng.shuffle(eval_examples)

    train_path = SFT_DIR / "train.jsonl"
    eval_path  = SFT_DIR / "eval.jsonl"

    _write_jsonl(train_path, train_examples)
    _write_jsonl(eval_path, eval_examples)

    # Token estimate (rough: 1 token ≈ 4 chars)
    total_chars = sum(
        len(m["content"])
        for ex in train_examples
        for m in ex["messages"]
    )
    est_tokens = total_chars // 4
    # gpt-4o-mini SFT: ~$0.004 per 1K training tokens (as of 2024)
    est_cost = est_tokens / 1000 * 0.004

    print(f"\nTrain: {len(train_examples)} examples → {train_path}")
    print(f"Eval:  {len(eval_examples)} examples → {eval_path}")
    print(f"Estimated training tokens: ~{est_tokens:,}  (~${est_cost:.2f})")


if __name__ == "__main__":
    main()
