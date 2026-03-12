"""
store.py - JSON-backed persistence for agent-azure pipeline.

Files:
    data/training_set.json      — regression emails per skill
    data/agent_guidelines.json  — agent behaviour patterns
    data/pipeline_results.json  — all pipeline run results

Public API:
    get_training(skill_name)                              -> list[dict]
    add_training_email(skill_name, subject, body, answer) -> bool
    load_guidelines()                                     -> list[dict]
    save_guidelines(guidelines)                           -> None
    append_run_result(result)                             -> None
    load_run_results()                                    -> list[dict]

Constants:
    REGRESSION_THRESHOLD = 3.5
    MAX_PER_SKILL        = 3
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

REGRESSION_THRESHOLD = 3.5
MAX_PER_SKILL = 3

_TRAINING_FILE  = DATA_DIR / "training_set.json"
_GUIDELINES_FILE = DATA_DIR / "agent_guidelines.json"
_RESULTS_FILE   = DATA_DIR / "pipeline_results.json"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Training set ──────────────────────────────────────────────────────────────

def get_training(skill_name: str) -> list[dict]:
    """Return all regression training emails for a skill."""
    data: dict = _read_json(_TRAINING_FILE, {})
    return data.get(skill_name, [])


def add_training_email(skill_name: str, subject: str, body: str, answer: str) -> bool:
    """Add an email to the training set if there is room (max MAX_PER_SKILL per skill).

    Returns True if added, False if the skill already has MAX_PER_SKILL entries.
    """
    data: dict = _read_json(_TRAINING_FILE, {})
    existing = data.get(skill_name, [])
    if len(existing) >= MAX_PER_SKILL:
        return False
    existing.append({"subject": subject, "body": body, "answer": answer})
    data[skill_name] = existing
    _write_json(_TRAINING_FILE, data)
    return True


# ── Agent guidelines ──────────────────────────────────────────────────────────

def load_guidelines() -> list[dict]:
    """Return all agent guidelines."""
    return _read_json(_GUIDELINES_FILE, [])


def save_guidelines(guidelines: list[dict]) -> None:
    """Overwrite the guidelines file."""
    _write_json(_GUIDELINES_FILE, guidelines)


def add_guideline(entry: dict) -> None:
    """Append a new guideline. Merges by topic if a matching entry already exists."""
    guidelines = load_guidelines()
    # Check for duplicate topic
    for i, g in enumerate(guidelines):
        if g.get("topic", "").lower() == entry.get("topic", "").lower():
            # Simple merge: replace with new entry (improver already does LLM merging)
            guidelines[i] = entry
            save_guidelines(guidelines)
            return
    guidelines.append(entry)
    save_guidelines(guidelines)


def guidelines_as_text() -> str:
    """Format all guidelines as a readable text block for injection into system prompts."""
    guidelines = load_guidelines()
    if not guidelines:
        return ""
    lines = ["## Agent Guidelines\n"]
    for g in guidelines:
        lines.append(f"**{g.get('topic', 'Guideline')}**")
        if g.get("trigger"):
            lines.append(f"When: {g['trigger']}")
        if g.get("instruction"):
            lines.append(f"Do: {g['instruction']}")
        lines.append("")
    return "\n".join(lines)


# ── Pipeline results ──────────────────────────────────────────────────────────

def append_run_result(result: dict) -> None:
    """Append one pipeline result to the results file."""
    results = _read_json(_RESULTS_FILE, [])
    results.append(result)
    _write_json(_RESULTS_FILE, results)


def load_run_results() -> list[dict]:
    """Return all stored pipeline results."""
    return _read_json(_RESULTS_FILE, [])
