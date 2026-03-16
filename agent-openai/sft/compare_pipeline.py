"""
compare_pipeline.py - Compare base vs. fine-tuned model using the full pipeline.

Runs each email through the complete classify → orchestrate → judge loop twice:
once with the base model and once with the fine-tuned model. Both runs use real
function tool calls and file_search KB retrieval — exactly as in production.

This is the honest SFT comparison. evaluate.py compared speculative text generation
against human ground truth without tool calls, which measured the wrong thing.
This script measures whether the fine-tuned model takes better actions, produces
more complete replies, and uses the right tools — the actual quality bar.

Run after fine_tune.py:
    python sft/compare_pipeline.py

Flags:
    --base-model        Base model ID (default: FAST_MODEL env var or gpt-4o-mini)
    --finetuned-model   Fine-tuned model ID (default: FINETUNED_MODEL env var or data/sft/model_id.txt)
    --limit             Number of emails to evaluate (default: 20)
    --offset            Skip first N emails (default: 0)
    --output            Output markdown report path (default: data/sft/compare_report.md)
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# Add agent-openai/ to sys.path so project modules (logger, classifier, etc.) are importable
# when this script is run from the sft/ subdirectory.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env before any module imports that read os.environ at module level
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from logger import get_logger  # noqa: E402
from openai import OpenAI  # noqa: E402

from classifier import classify  # noqa: E402
from evaluator import judge  # noqa: E402
import orchestrator_agent  # noqa: E402
import specialist_agents  # noqa: E402
from orchestrator_agent import orchestrate  # noqa: E402
from tracing import setup_tracing  # noqa: E402

log = get_logger(__name__)

DATA_DIR  = Path(__file__).parent.parent / "data"
EMAILS_CSV = DATA_DIR / "emails.csv"
SFT_DIR   = DATA_DIR / "sft"


def _load_emails(limit: int, offset: int) -> list[dict]:
    emails = []
    skipped = 0
    with open(EMAILS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("language") != "en":
                continue
            if skipped < offset:
                skipped += 1
                continue
            if len(emails) >= limit:
                break
            emails.append({
                "subject": row.get("subject", ""),
                "body":    row.get("body", ""),
                "queue":   row.get("queue", ""),
                "answer":  row.get("answer", ""),
            })
    return emails


def _set_model(model: str) -> None:
    """Patch the module-level MODEL constant in both specialist and orchestrator modules.

    Both modules capture MODEL from os.environ at import time. We patch them directly
    so both runs in the same process use the correct model without re-importing.
    """
    specialist_agents.MODEL = model
    orchestrator_agent.MODEL = model


def _run_one(
    client: OpenAI,
    model: str,
    email: dict,
    vector_store_id: str,
    tracer,
    retries: int = 2,
) -> tuple[str, list[str], bool, dict | None]:
    """Run a single email through the full pipeline with the given model.

    Returns (final_reply, tools_called, escalated, scores_or_None).
    scores is None when there is no ground truth or no reply generated.
    Retries on transient server errors (status=failed, code=server_error).
    """
    _set_model(model)

    classification = classify(client, email)
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            result = orchestrate(client, email, classification, vector_store_id, tracer)
            break
        except RuntimeError as exc:
            last_exc = exc
            if attempt < retries:
                print(f" [retry {attempt + 1}/{retries}] {exc}")
            else:
                raise RuntimeError(f"Failed after {1 + retries} attempts: {exc}") from exc

    all_tools = [t for r in result.results for t in r.tools_called]
    ground_truth = email.get("answer", "")
    scores = None
    if ground_truth and result.final_reply:
        scores = judge(client, email, ground_truth, result.final_reply)

    return result.final_reply or "", all_tools, result.escalated, scores


def _write_report(
    path: Path,
    rows: list[dict],
    base_model: str,
    ft_model: str,
    started_at: str,
    total: int,
) -> None:
    """Write the full comparison report. Called after every email."""
    n = len(rows)
    if not n:
        return

    scored = [r for r in rows if r["base"]["scores"] and r["ft"]["scores"]]

    def col_avg(side: str, key: str) -> float:
        vals = [r[side]["scores"][key] for r in scored if r[side]["scores"]]
        return sum(vals) / len(vals) if vals else 0.0

    progress = f"{n}/{total}"
    lines = [
        f"# SFT Pipeline Comparison Report — {started_at}",
        "",
        f"**Base model:** `{base_model}`",
        f"**Fine-tuned model:** `{ft_model}`",
        f"**Emails evaluated:** {progress}  ({len(scored)} scored)",
        "",
        "Both models run the full pipeline: classify → orchestrate with function tools "
        "+ file_search → LLM judge. Scores reflect real tool-call behaviour.",
        "",
        "## Summary (so far)",
        "",
        "| Metric | Base | Fine-tuned | Δ |",
        "|--------|------|------------|---|",
    ]
    for metric in ["action", "completeness", "tone", "avg"]:
        b = col_avg("base", metric)
        f = col_avg("ft", metric)
        delta = f - b
        sign = "+" if delta >= 0 else ""
        lines.append(f"| {metric} | {b:.2f} | {f:.2f} | {sign}{delta:.2f} |")

    # Tool call stats
    base_tool_counts = [len(r["base"]["tools"]) for r in rows]
    ft_tool_counts   = [len(r["ft"]["tools"])   for r in rows]
    base_esc = sum(1 for r in rows if r["base"]["escalated"])
    ft_esc   = sum(1 for r in rows if r["ft"]["escalated"])

    lines += [
        f"| tool calls (mean) | {sum(base_tool_counts)/n:.1f} | {sum(ft_tool_counts)/n:.1f} "
        f"| {(sum(ft_tool_counts)-sum(base_tool_counts))/n:+.1f} |",
        f"| escalation rate | {base_esc/n:.0%} | {ft_esc/n:.0%} | — |",
        "",
        "## Per-email results",
        "",
    ]

    for i, row in enumerate(rows, 1):
        b  = row["base"]
        ft = row["ft"]
        bs = b["scores"] or {}
        fs = ft["scores"] or {}
        lines.append(f"### [{i}] {row['subject'][:70]}")
        lines.append(
            f"**Base avg:** {bs.get('avg', 0):.1f}  "
            f"**Fine-tuned avg:** {fs.get('avg', 0):.1f}  "
            f"**Δ:** {fs.get('avg', 0) - bs.get('avg', 0):+.1f}"
        )
        lines.append(
            f"**Base tools:** {', '.join(b['tools']) or '(none)'}  "
            f"| **FT tools:** {', '.join(ft['tools']) or '(none)'}"
        )
        lines.append(f"**Base comment:** {bs.get('comment', '')}  "
                     f"**FT comment:** {fs.get('comment', '')}")
        lines.append("")
        lines.append("<details><summary>Base reply</summary>")
        lines.append("")
        lines.append(b["reply"].strip())
        lines.append("")
        lines.append("</details>")
        lines.append("")
        lines.append("<details><summary>Fine-tuned reply</summary>")
        lines.append("")
        lines.append(ft["reply"].strip())
        lines.append("")
        lines.append("</details>")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare base vs fine-tuned model using the full pipeline")
    parser.add_argument("--base-model",      default=os.environ.get("FAST_MODEL", "gpt-4o-mini"))
    parser.add_argument("--finetuned-model", default="")
    parser.add_argument("--limit",           type=int, default=20)
    parser.add_argument("--offset",          type=int, default=0)
    parser.add_argument("--output",          type=Path, default=SFT_DIR / "compare_report.md")
    args = parser.parse_args()

    # Resolve fine-tuned model ID
    ft_model = (
        args.finetuned_model
        or os.environ.get("FINETUNED_MODEL", "")
        or ((SFT_DIR / "model_id.txt").read_text().strip() if (SFT_DIR / "model_id.txt").exists() else "")
    )
    if not ft_model:
        print("ERROR: fine-tuned model ID not found. Set FINETUNED_MODEL in .env or run fine_tune.py first.")
        sys.exit(1)

    vector_store_id = os.environ.get("VECTOR_STORE_ID", "")
    if not vector_store_id:
        print("ERROR: VECTOR_STORE_ID not set. Run kb_setup.py first.")
        sys.exit(1)

    client    = OpenAI()
    tracer    = setup_tracing()
    emails    = _load_emails(args.limit, args.offset)
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"Pipeline comparison — {len(emails)} email(s)")
    print(f"  Base:        {args.base_model}")
    print(f"  Fine-tuned:  {ft_model}")
    print(f"  Report:      {args.output}")
    print("=" * 70)

    rows: list[dict] = []

    for i, email in enumerate(emails, args.offset + 1):
        subject = email.get("subject") or "(no subject)"
        print(f"\n  [{i}/{args.offset + len(emails)}] {subject[:65]}")

        print(f"    base ({args.base_model}) ...", end="", flush=True)
        base_reply, base_tools, base_esc, base_scores = _run_one(
            client, args.base_model, email, vector_store_id, tracer
        )
        base_avg = base_scores["avg"] if base_scores else 0.0
        print(f" avg={base_avg:.1f}  tools={base_tools}")

        print(f"    ft   ({ft_model[:30]}) ...", end="", flush=True)
        ft_reply, ft_tools, ft_esc, ft_scores = _run_one(
            client, ft_model, email, vector_store_id, tracer
        )
        ft_avg = ft_scores["avg"] if ft_scores else 0.0
        print(f" avg={ft_avg:.1f}  tools={ft_tools}")

        delta = ft_avg - base_avg
        print(f"    Δ={delta:+.1f}  base_comment={base_scores.get('comment','') if base_scores else ''}")

        rows.append({
            "subject": subject,
            "base": {
                "reply":     base_reply,
                "tools":     base_tools,
                "escalated": base_esc,
                "scores":    base_scores,
            },
            "ft": {
                "reply":     ft_reply,
                "tools":     ft_tools,
                "escalated": ft_esc,
                "scores":    ft_scores,
            },
        })

        _write_report(args.output, rows, args.base_model, ft_model, started_at, len(emails))

    # Final summary
    scored = [r for r in rows if r["base"]["scores"] and r["ft"]["scores"]]
    if scored:
        def overall(side: str) -> float:
            return sum(r[side]["scores"]["avg"] for r in scored) / len(scored)

        base_avg = overall("base")
        ft_avg   = overall("ft")
        delta    = ft_avg - base_avg

        print(f"\n{'='*50}")
        print(f"RESULTS ({len(scored)} emails scored)")
        print(f"  Base model avg:       {base_avg:.2f}/5")
        print(f"  Fine-tuned model avg: {ft_avg:.2f}/5")
        print(f"  Delta:                {delta:+.2f}")
        print(f"{'='*50}")

    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
