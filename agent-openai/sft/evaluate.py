"""
evaluate.py - Compare base vs. fine-tuned model on the held-out eval set.

Both models use the OpenAI Assistants API with file_search for KB retrieval —
the same architecture used in production. The difference is the system prompt:

  Base model (gpt-4o-mini):      file_search (KB) + guidelines in system prompt
  Fine-tuned model (ft:...):     file_search (KB) + NO guidelines in system prompt

If the fine-tuned model scores comparably to the base model without needing
the guidelines in its prompt, the training successfully baked in the behaviour.

Run after fine_tune.py:
    python sft/evaluate.py

Flags:
    --vector-store-id   OpenAI vector store ID (default: VECTOR_STORE_ID env var)
    --finetuned-model   Fine-tuned model ID (default: FINETUNED_MODEL env var or data/sft/model_id.txt)
    --base-model        Base model for comparison (default: gpt-4o-mini)
    --eval-file         Path to eval JSONL (default: data/sft/eval.jsonl)
    --limit             Max examples to evaluate (default: all)
    --output            Output markdown report path (default: data/sft/eval_report.md)
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_utils import run_with_tool_dispatch
from kb_setup import build_guidelines_markdown
from tools import ALL_TOOLS, TOOL_DEFINITIONS

load_dotenv(Path(__file__).parent.parent / ".env")

SFT_DIR   = Path(__file__).parent.parent / "data" / "sft"
EVAL_FILE = SFT_DIR / "eval.jsonl"

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


def _load_eval_examples(path: Path, limit: int) -> list[dict]:
    examples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            examples.append(json.loads(line))
            if limit and len(examples) >= limit:
                break
    return examples


def _run_assistant(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_msg: str,
    vector_store_id: str,
) -> str:
    """Create a one-shot assistant with file_search and return the reply text."""
    assistant = client.beta.assistants.create(
        model=model,
        instructions=system_prompt,
        tools=[{"type": "file_search"}],
        tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
    )
    thread = client.beta.threads.create()
    try:
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=user_msg,
        )
        run = run_with_tool_dispatch(client, thread.id, assistant.id, tool_fns={})
        if run.status not in ("completed", "incomplete"):
            return ""

        messages = client.beta.threads.messages.list(thread_id=thread.id, order="desc")
        for msg in messages:
            if msg.role == "assistant":
                for part in msg.content:
                    if part.type == "text":
                        text = part.text.value.strip()
                        for ann in getattr(part.text, "annotations", []):
                            if hasattr(ann, "text"):
                                text = text.replace(ann.text, "")
                        return text
    finally:
        client.beta.threads.delete(thread.id)
        client.beta.assistants.delete(assistant.id)
    return ""


def _judge(client: OpenAI, user_msg: str, ground_truth: str, generated: str, judge_model: str) -> dict:
    user = (
        f"EMAIL\n{user_msg[:600]}\n\n"
        f"GROUND TRUTH REPLY\n{ground_truth[:600]}\n\n"
        f"GENERATED REPLY\n{generated[:2000]}"
    )
    resp = client.chat.completions.create(
        model=judge_model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user",   "content": user},
        ],
        response_format={"type": "json_object"},
    )
    scores = json.loads(resp.choices[0].message.content or "{}")
    scores["avg"] = (scores.get("action", 3) + scores.get("completeness", 3) + scores.get("tone", 3)) / 3
    return scores


def _make_minimal_system(example: dict) -> str:
    """Extract a system prompt that keeps KB context but strips guidelines.

    The eval.jsonl system prompt contains both KB entries and guidelines.
    For the fine-tuned model we want KB-only (guidelines baked in via training).
    """
    system = ""
    for msg in example["messages"]:
        if msg["role"] == "system":
            system = msg["content"]
            break

    # Strip the Agent Behaviour Guidelines section
    if "## Agent Behaviour Guidelines" in system:
        system = system[:system.index("## Agent Behaviour Guidelines")].rstrip()

    # Strip the KB section too — we're using file_search for KB retrieval
    if "## Knowledge Base" in system:
        system = system[:system.index("## Knowledge Base")].rstrip()

    return system.strip() or "You are a customer support specialist."


def _make_base_system(example: dict) -> str:
    """Extract a system prompt that includes guidelines but strips inline KB (uses file_search)."""
    system = ""
    for msg in example["messages"]:
        if msg["role"] == "system":
            system = msg["content"]
            break

    # Strip inline KB — file_search handles retrieval at runtime
    if "## Knowledge Base" in system:
        system = system[:system.index("## Knowledge Base")].rstrip()
        # Re-attach guidelines if they were after KB
        guidelines_md = build_guidelines_markdown()
        if guidelines_md:
            system = system + f"\n\n## Agent Behaviour Guidelines\n\n{guidelines_md}"

    return system.strip() or "You are a customer support specialist."


def _write_report(path: Path, rows: list[dict], base_model: str, ft_model: str) -> None:
    n = len(rows)
    if not n:
        return

    def avg(key, model_key):
        vals = [r[model_key]["scores"].get(key, 3) for r in rows if model_key in r]
        return sum(vals) / len(vals) if vals else 0

    lines = [
        f"# SFT Evaluation Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**Base model:** {base_model} (file_search + guidelines in prompt)",
        f"**Fine-tuned model:** {ft_model} (file_search only, no guidelines in prompt)",
        f"**Examples evaluated:** {n}",
        "",
        "## Summary",
        "",
        "| Metric | Base | Fine-tuned | Δ |",
        "|--------|------|------------|---|",
    ]
    for metric in ["action", "completeness", "tone", "avg"]:
        b = avg(metric, "base")
        f = avg(metric, "finetuned")
        delta = f - b
        sign = "+" if delta >= 0 else ""
        lines.append(f"| {metric} | {b:.2f} | {f:.2f} | {sign}{delta:.2f} |")

    lines += ["", "## Per-example results", ""]
    for i, row in enumerate(rows, 1):
        b = row.get("base", {})
        ft = row.get("finetuned", {})
        lines.append(f"### [{i}] {row['subject'][:70]}")
        lines.append(f"**Base avg:** {b.get('scores', {}).get('avg', 0):.1f}  "
                     f"**Fine-tuned avg:** {ft.get('scores', {}).get('avg', 0):.1f}")
        lines.append(f"**Base comment:** {b.get('scores', {}).get('comment', '')}")
        lines.append(f"**Fine-tuned comment:** {ft.get('scores', {}).get('comment', '')}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare base vs fine-tuned model on eval set")
    parser.add_argument("--vector-store-id",  default=os.environ.get("VECTOR_STORE_ID", ""))
    parser.add_argument("--finetuned-model",  default="")
    parser.add_argument("--base-model",       default=os.environ.get("FAST_MODEL", "gpt-4o-mini"))
    parser.add_argument("--judge-model",      default=os.environ.get("FAST_MODEL", "gpt-4o-mini"))
    parser.add_argument("--eval-file",        type=Path, default=EVAL_FILE)
    parser.add_argument("--limit",            type=int, default=0)
    parser.add_argument("--output",           type=Path, default=SFT_DIR / "eval_report.md")
    args = parser.parse_args()

    # Resolve fine-tuned model ID
    ft_model = (
        args.finetuned_model
        or os.environ.get("FINETUNED_MODEL", "")
        or (SFT_DIR / "model_id.txt").read_text().strip()
        if (SFT_DIR / "model_id.txt").exists() else ""
    )
    if not ft_model:
        print("ERROR: fine-tuned model ID not found. Set FINETUNED_MODEL in .env or run fine_tune.py first.")
        sys.exit(1)
    if not args.vector_store_id:
        print("ERROR: VECTOR_STORE_ID not set. Run kb_setup.py first.")
        sys.exit(1)

    client = OpenAI()
    examples = _load_eval_examples(args.eval_file, args.limit)
    print(f"Evaluating {len(examples)} examples")
    print(f"  Base:        {args.base_model}")
    print(f"  Fine-tuned:  {ft_model}")
    print(f"  Vector store: {args.vector_store_id}\n")

    rows = []
    base_scores_all  = []
    ft_scores_all    = []

    for i, example in enumerate(examples, 1):
        # Extract user message and ground truth from the example
        user_msg     = next(m["content"] for m in example["messages"] if m["role"] == "user")
        ground_truth = next(m["content"] for m in example["messages"] if m["role"] == "assistant")
        subject = user_msg.split("\n")[0].replace("Subject: ", "")[:70]

        print(f"  [{i}/{len(examples)}] {subject}")

        base_system = _make_base_system(example)
        ft_system   = _make_minimal_system(example)

        base_reply = _run_assistant(client, args.base_model, base_system, user_msg, args.vector_store_id)
        ft_reply   = _run_assistant(client, ft_model, ft_system, user_msg, args.vector_store_id)

        base_scores = _judge(client, user_msg, ground_truth, base_reply, args.judge_model)
        ft_scores   = _judge(client, user_msg, ground_truth, ft_reply, args.judge_model)

        print(f"       base avg={base_scores['avg']:.1f}  ft avg={ft_scores['avg']:.1f}  "
              f"Δ={ft_scores['avg'] - base_scores['avg']:+.1f}")

        base_scores_all.append(base_scores["avg"])
        ft_scores_all.append(ft_scores["avg"])

        rows.append({
            "subject":   subject,
            "base":      {"reply": base_reply,  "scores": base_scores},
            "finetuned": {"reply": ft_reply,    "scores": ft_scores},
        })

    n = len(rows)
    base_avg = sum(base_scores_all) / n
    ft_avg   = sum(ft_scores_all)   / n
    delta    = ft_avg - base_avg

    print(f"\n{'='*50}")
    print(f"RESULTS ({n} examples)")
    print(f"  Base model avg:       {base_avg:.2f}/5")
    print(f"  Fine-tuned model avg: {ft_avg:.2f}/5")
    print(f"  Delta:                {delta:+.2f}")
    print(f"{'='*50}")

    _write_report(args.output, rows, args.base_model, ft_model)
    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
