"""
evaluate.py - Compare base vs. fine-tuned model on the held-out eval set.

Both models use the Responses API with file_search for KB retrieval.
The difference is the system prompt:

  Base model (gpt-4o-mini):      file_search (KB) + guidelines in system prompt
  Fine-tuned model (ft:...):     file_search (KB) only — NO guidelines in system prompt

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

load_dotenv(Path(__file__).parent.parent / ".env")

SFT_DIR   = Path(__file__).parent.parent / "data" / "sft"
EVAL_FILE = SFT_DIR / "eval.jsonl"

JUDGE_SYSTEM = """You are an evaluation assistant for a customer support AI.
You will be given:
  - An email (subject + body)
  - A ground-truth human reply (what a human agent actually sent)
  - A generated reply (what the AI agent produced)

Score the generated reply on three dimensions, each 1–5:
  intent       - Did the reply express the right response strategy (offer refund, escalate,
                 ask for clarification, explain policy, etc.)? Note: no tools are executed
                 in this evaluation — score whether the reply COMMUNICATES the right intent,
                 not whether an action was executed.
                 5=correct intent clearly expressed, 1=wrong or missing intent
  completeness - Did it address the customer's core concern with key details?
                 5=fully addressed, 1=missed the point
  tone         - Was the tone appropriate (warm, clear, professional)?
                 5=excellent, 1=cold/confusing/inappropriate

Return JSON with keys: intent, completeness, tone, comment
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


def _run_model(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_msg: str,
    vector_store_id: str,
) -> str:
    """Call the Responses API with file_search and return the reply text."""
    response = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=user_msg,
        tools=[{"type": "file_search", "vector_store_ids": [vector_store_id]}],
    )
    return response.output_text.strip()


def _judge(client: OpenAI, user_msg: str, ground_truth: str, generated: str, judge_model: str) -> dict:
    user = (
        f"EMAIL\n{user_msg[:600]}\n\n"
        f"GROUND TRUTH REPLY\n{ground_truth[:600]}\n\n"
        f"GENERATED REPLY\n{generated[:2000]}\n\n"
        f"Respond with a JSON object."
    )
    response = client.responses.create(
        model=judge_model,
        instructions=JUDGE_SYSTEM,
        input=user,
        text={"format": {"type": "json_object"}},
    )
    scores = json.loads(response.output_text)
    scores["avg"] = (scores.get("intent", 3) + scores.get("completeness", 3) + scores.get("tone", 3)) / 3
    return scores


def _make_minimal_system(example: dict) -> str:
    """Return base instructions only — KB and guidelines both stripped.

    Used for the fine-tuned model: guidelines are baked in via training,
    KB is retrieved at runtime via file_search.
    """
    system = next((m["content"] for m in example["messages"] if m["role"] == "system"), "")
    # Strip inline KB — file_search handles retrieval at runtime
    if "## Knowledge Base" in system:
        system = system[:system.index("## Knowledge Base")].rstrip()
    # Strip guidelines — baked into fine-tuned weights
    if "## Agent Behaviour Guidelines" in system:
        system = system[:system.index("## Agent Behaviour Guidelines")].rstrip()
    return system.strip() or "You are a customer support specialist."


def _make_base_system(example: dict) -> str:
    """Return base instructions + guidelines — KB stripped (file_search handles it).

    Used for the base model comparison: guidelines in prompt, KB via file_search.
    """
    system = next((m["content"] for m in example["messages"] if m["role"] == "system"), "")
    # Strip inline KB — file_search handles retrieval at runtime
    if "## Knowledge Base" in system:
        guidelines_start = system.find("## Agent Behaviour Guidelines")
        kb_start = system.index("## Knowledge Base")
        guidelines = system[guidelines_start:] if guidelines_start != -1 else ""
        system = system[:kb_start].rstrip()
        if guidelines:
            system = system + "\n\n" + guidelines
    return system.strip() or "You are a customer support specialist."


def _write_report(path: Path, rows: list[dict], base_model: str, ft_model: str,
                  started_at: str, total: int) -> None:
    """Rewrite the full report from accumulated rows. Called after every example."""
    n = len(rows)
    if not n:
        return

    def avg(key, model_key):
        vals = [r[model_key]["scores"].get(key, 3) for r in rows if model_key in r]
        return sum(vals) / len(vals) if vals else 0

    progress = f"{n}/{total}" if total else str(n)
    lines = [
        f"# SFT Evaluation Report — {started_at}",
        "",
        f"**Base model:** {base_model} (file_search + guidelines in prompt)",
        f"**Fine-tuned model:** {ft_model} (file_search only, no guidelines in prompt)",
        f"**Examples evaluated:** {progress}",
        "",
        "## Summary (so far)",
        "",
        "| Metric | Base | Fine-tuned | Δ |",
        "|--------|------|------------|---|",
    ]
    for metric in ["intent", "completeness", "tone", "avg"]:
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
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Evaluating {len(examples)} examples")
    print(f"  Base:        {args.base_model}")
    print(f"  Fine-tuned:  {ft_model}")
    print(f"  Vector store: {args.vector_store_id}")
    print(f"  Report:       {args.output}\n")

    rows = []
    base_scores_all  = []
    ft_scores_all    = []

    for i, example in enumerate(examples, 1):
        user_msg     = next(m["content"] for m in example["messages"] if m["role"] == "user")
        ground_truth = next(m["content"] for m in example["messages"] if m["role"] == "assistant")
        subject = user_msg.split("\n")[0].replace("Subject: ", "")[:70]

        print(f"  [{i}/{len(examples)}] {subject}")

        base_system = _make_base_system(example)
        ft_system   = _make_minimal_system(example)

        base_reply = _run_model(client, args.base_model, base_system, user_msg, args.vector_store_id)
        ft_reply   = _run_model(client, ft_model, ft_system, user_msg, args.vector_store_id)

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

        # Write after every example so the report is readable if interrupted
        _write_report(args.output, rows, args.base_model, ft_model,
                      started_at=started_at, total=len(examples))

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
    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
