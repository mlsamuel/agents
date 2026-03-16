"""
evaluator.py - Managed evaluation using Azure AI Evaluation SDK.

Public API:
    judge(email, ground_truth, generated) -> dict
        Returns: {groundedness: 1-5, relevance: 1-5, coherence: 1-5, fluency: 1-5,
                  avg: float, comment: str}

    init_output(path)          -> None
    append_section(section, path) -> None
"""

import os
import re
from datetime import datetime
from pathlib import Path

from azure.ai.evaluation import (
    AzureOpenAIModelConfiguration,
    CoherenceEvaluator,
    FluencyEvaluator,
    RelevanceEvaluator,
    GroundednessEvaluator,
)

from logger import get_logger

log = get_logger(__name__)


def _get_model_config() -> AzureOpenAIModelConfiguration:
    base_endpoint = re.sub(r"/api/projects/.*$", "", os.environ["PROJECT_ENDPOINT"])
    return AzureOpenAIModelConfiguration(
        azure_endpoint=base_endpoint,
        azure_deployment=os.environ.get("FAST_MODEL", "gpt-4o-mini"),
        api_version="2024-08-01-preview",
    )


def judge(email: dict, ground_truth: str, generated: str) -> dict:
    """Score the generated reply using Azure AI Evaluation managed evaluators.

    Returns a dict with keys: groundedness, relevance, coherence, fluency, avg, comment.
    """
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:800]
    query = f"Subject: {subject}\n\n{body}"

    model_config = _get_model_config()

    dims = ["groundedness", "relevance", "coherence", "fluency"]
    raw = {}
    raw["groundedness"] = GroundednessEvaluator(model_config)(
        query=query, response=generated, context=ground_truth
    )
    raw["relevance"] = RelevanceEvaluator(model_config)(
        query=query, response=generated
    )
    raw["coherence"] = CoherenceEvaluator(model_config)(
        query=query, response=generated
    )
    raw["fluency"] = FluencyEvaluator(model_config)(
        response=generated
    )

    scores = {k: raw[k][k] for k in dims}
    scores["avg"] = sum(scores[k] for k in dims) / len(dims)

    lowest_key = min(dims, key=lambda k: scores[k])
    lowest_val = scores[lowest_key]
    reason = raw[lowest_key].get(f"{lowest_key}_reason", "")
    scores["comment"] = reason if reason else (
        f"low {lowest_key} ({lowest_val:.0f}/5)" if lowest_val < 4 else "none"
    )

    return scores


# ── Output helpers ────────────────────────────────────────────────────────────

def _section_lines(s: dict) -> list[str]:
    score = s["score"]
    return [
        "---",
        f"## [{s['index']}] {s['subject']}",
        f"**Queue:** {s['queue']} | **Type:** {s['type']} | **Priority:** {s['priority']}",
        f"**Skills:** {s['skills']}  **Tools:** {s['tools']}"
        + (f"  **KB:** {', '.join(s['files_searched'])}" if s.get("files_searched") else ""),
        f"**Scores:** groundedness={score['groundedness']}/5  relevance={score['relevance']}/5  "
        f"coherence={score['coherence']}/5  fluency={score['fluency']}/5  avg={s['avg']:.1f}",
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
    ]


def init_output(path: str = "eval_output.md") -> None:
    header = f"# Eval output — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    Path(path).write_text(header, encoding="utf-8")


def append_section(section: dict, path: str = "eval_output.md") -> None:
    lines = _section_lines(section)
    with Path(path).open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
