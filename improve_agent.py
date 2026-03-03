"""
improve_agent.py - Eval-driven skill/KB improvement pipeline.

Reads eval_results.json produced by eval_agent.py, identifies low-scoring emails,
groups them by skill, and asks Claude Sonnet to propose targeted improvements:
  - skill_edit: rewrite an existing skill .md
  - kb_entry:   add an entry to knowledge_base.json
  - new_skill:  create a new skill .md file

Proposals are always written to improve_proposals.md.
With --apply: proposals are applied to disk, then the same emails are re-evaluated
and a before/after score delta is printed.

Usage:
    python improve_agent.py                    # analyse eval_results.json, min-score 4.0
    python improve_agent.py --min-score 4.5
    python improve_agent.py --apply            # apply proposals + re-run eval
    python improve_agent.py --eval path/to/custom.json
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from email_stream import email_stream
from classifier_agent import classify
from orchestrator_agent import orchestrate
from eval_agent import judge, _write_output, _write_json
from logger import get_logger

log = get_logger(__name__)

load_dotenv()

SKILLS_DIR   = Path(__file__).parent / "skills"
KB_PATH      = Path(__file__).parent / "data" / "knowledge_base.json"
IMPROVE_MODEL = "claude-sonnet-4-6"

QUEUE_TO_KEY = {
    "Technical Support":              "technical_support",
    "Billing and Payments":           "billing",
    "Returns and Exchanges":          "returns",
    "Sales and Pre-Sales":            "general",
    "General":                        "general",
    "IT Support":                     "technical_support",
    "Service Outages and Maintenance":"technical_support",
}

IMPROVE_SYSTEM = """\
You are an expert at improving customer support AI agent skills.

You will receive:
1. A skill .md file (full content) used by an agent to handle support emails
2. One or more examples where the agent scored below threshold, with scores,
   eval comments, ground truth replies, and the agent's generated replies
3. The current knowledge base entries for this category

Propose the minimum changes needed to close the observed gaps.
For each proposal choose the most appropriate type:
  skill_edit  — the workflow instructions or reply format need clarification/expansion
  kb_entry    — the agent lacked specific factual information visible in the ground truth
  new_skill   — a whole new workflow type is clearly needed (rare)

Rules:
- Only propose changes directly evidenced by the eval comments and ground truth
- For skill_edit and new_skill: provide the COMPLETE .md file content (full rewrite).
  Preserve all existing security notes, frontmatter fields, and format rules.
- For kb_entry: derive the answer text from the ground truth reply; do not invent facts.
  Assign IDs by incrementing from the highest existing ID in that category.
- Be conservative — prefer a focused skill_edit over multiple proposals when it suffices.
- Respond with valid JSON only, no markdown wrapper, matching this exact schema:
{
  "proposals": [
    {
      "type": "skill_edit",
      "skill_file": "skills/general/general_inquiry.md",
      "rationale": "one sentence explaining why",
      "new_content": "--- full .md content here ---"
    },
    {
      "type": "kb_entry",
      "rationale": "one sentence explaining why",
      "entry": {
        "id": "general-004",
        "category": "general",
        "topic": "...",
        "question": "...",
        "answer": "...",
        "keywords": ["..."]
      }
    },
    {
      "type": "new_skill",
      "skill_file": "skills/general/new_skill.md",
      "rationale": "one sentence explaining why",
      "new_content": "--- full .md content here ---"
    }
  ]
}
"""


# ── Loaders ─────────────────────────────────────────────────────────────────

def _load_eval(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def _load_all_skills() -> dict[str, dict]:
    """Return {skill_name: {path, content}} for all .md files under skills/."""
    skills = {}
    for md in SKILLS_DIR.glob("**/*.md"):
        content = md.read_text()
        # Extract name from frontmatter if present, else use stem
        name = md.stem
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                import yaml
                meta = yaml.safe_load(parts[1]) or {}
                name = meta.get("name", name)
        skills[name] = {"path": md, "content": content}
    return skills


def _load_kb() -> list[dict]:
    with open(KB_PATH) as f:
        return json.load(f)


def _kb_for_category(kb: list[dict], category: str) -> list[dict]:
    return [e for e in kb if e.get("category") == category]


def _next_kb_id(kb: list[dict], category: str) -> str:
    """Return the next unused ID for the given category (e.g. 'general-004')."""
    existing = [
        int(e["id"].split("-")[1])
        for e in kb
        if e.get("category") == category and "-" in e.get("id", "")
    ]
    n = max(existing, default=0) + 1
    return f"{category}-{n:03d}"


# ── Proposal generation ──────────────────────────────────────────────────────

def _build_examples_text(records: list[dict]) -> str:
    parts = []
    for r in records:
        s = r["score"]
        parts.append(
            f"--- Example [{r['index']}]: {r['subject']} ---\n"
            f"Scores: action={s['action']}/5  completeness={s['completeness']}/5"
            f"  tone={s['tone']}/5  avg={r['avg']:.1f}\n"
            f"Eval comment: {s['comment']}\n\n"
            f"Ground truth reply:\n{r['ground_truth'][:800]}\n\n"
            f"Generated reply:\n{r['generated'][:800]}\n"
        )
    return "\n".join(parts)


def _generate_proposals(
    client: anthropic.Anthropic,
    skill_name: str,
    skill_info: dict | None,
    records: list[dict],
    kb: list[dict],
) -> list[dict]:
    """Call Claude to generate improvement proposals for one skill group."""
    category = QUEUE_TO_KEY.get(records[0]["queue"], "general")
    kb_entries = _kb_for_category(kb, category)

    skill_content = skill_info["content"] if skill_info else "(skill file not found)"
    skill_file    = str(skill_info["path"].relative_to(Path(__file__).parent)) if skill_info else f"skills/{category}/{skill_name}.md"

    user_msg = (
        f"## Skill file: {skill_file}\n\n"
        f"```\n{skill_content}\n```\n\n"
        f"## Failing examples (avg < threshold)\n\n"
        f"{_build_examples_text(records)}\n\n"
        f"## Current knowledge base entries (category: {category})\n\n"
        f"```json\n{json.dumps(kb_entries, indent=2)}\n```\n\n"
        f"Propose improvements. Remember: respond with JSON only."
    )

    response = client.messages.create(
        model=IMPROVE_MODEL,
        max_tokens=4096,
        system=IMPROVE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()
    data = json.loads(raw)
    return data.get("proposals", [])


# ── Proposal output ──────────────────────────────────────────────────────────

def _write_proposals(all_proposals: list[dict], path: str = "improve_proposals.md") -> None:
    lines = [
        f"# Improvement proposals",
        f"*{len(all_proposals)} proposal(s)*",
        "",
    ]
    for i, p in enumerate(all_proposals, 1):
        ptype = p["type"].upper().replace("_", " ")
        target = p.get("skill_file") or p.get("entry", {}).get("id", "kb")
        lines += [
            f"---",
            f"## Proposal {i} — {ptype}: {target}",
            f"**Rationale:** {p['rationale']}",
            "",
        ]
        if p["type"] in ("skill_edit", "new_skill"):
            lines += [
                "```md",
                p["new_content"],
                "```",
                "",
            ]
        else:
            lines += [
                "```json",
                json.dumps(p["entry"], indent=2),
                "```",
                "",
            ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Proposals written to {path}")


# ── Apply ────────────────────────────────────────────────────────────────────

def _apply_proposals(all_proposals: list[dict]) -> None:
    kb = _load_kb()
    base = Path(__file__).parent

    for p in all_proposals:
        ptype = p["type"]
        if ptype in ("skill_edit", "new_skill"):
            target = base / p["skill_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p["new_content"], encoding="utf-8")
            action = "Updated" if ptype == "skill_edit" else "Created"
            print(f"  {action}: {p['skill_file']}")

        elif ptype == "kb_entry":
            entry = p["entry"]
            # Assign a fresh ID if the proposed one collides
            existing_ids = {e["id"] for e in kb}
            if entry.get("id") in existing_ids:
                entry["id"] = _next_kb_id(kb, entry.get("category", "general"))
            kb.append(entry)
            print(f"  KB entry added: {entry['id']} — {entry.get('topic', '')}")

    KB_PATH.write_text(json.dumps(kb, indent=2), encoding="utf-8")
    print(f"  Knowledge base saved to {KB_PATH}")


# ── Re-evaluate ──────────────────────────────────────────────────────────────

def _reeval(client: anthropic.Anthropic, failing: list[dict]) -> list[dict]:
    """Re-run orchestrate + judge for the same emails. Returns full output sections."""
    target_indices = {r["index"] for r in failing}
    updated = []

    seen: set[int] = set()
    max_index = max(target_indices)
    stream = email_stream(language="en", limit=None, offset=0)
    for i, email in enumerate(stream, 1):
        if i > max_index:
            break
        if i not in target_indices:
            continue
        seen.add(i)
        ground_truth = email.get("answer") or ""
        if not ground_truth:
            if seen == target_indices:
                break
            continue
        try:
            classification = classify(client, email)
            result = orchestrate(classification, email)
            generated = result.final_reply or ""
            internal_summary = result.results[0].internal_summary if result.results else ""
            skills_str = ", ".join(s.skill_used for s in result.results)
            all_tools = [c["tool"] for s in result.results for c in s.tool_calls]
            tools_str = ", ".join(all_tools) if all_tools else "(none)"
            score = judge(client, email, ground_truth, generated)
            avg = (score["action"] + score["completeness"] + score["tone"]) / 3
            updated.append({
                "index": i,
                "subject": email.get("subject") or "(no subject)",
                "body": email.get("body") or "",
                "queue": classification["queue"],
                "type": classification["type"],
                "priority": classification["priority"],
                "skills": skills_str,
                "tools": tools_str,
                "ground_truth": ground_truth,
                "generated": generated,
                "internal_summary": internal_summary,
                "score": score,
                "avg": avg,
            })
        except Exception as exc:
            log.error("Re-eval failed for email %d: %s", i, exc)
        if seen == target_indices:
            break

    return updated


def _print_delta(before: list[dict], after: list[dict]) -> None:
    after_map = {r["index"]: r for r in after}
    print("\nBefore / After scores:")
    print(f"{'idx':>4}  {'skill':<22}  {'before':>6}  {'after':>5}  {'delta':>5}")
    print("-" * 52)
    for b in before:
        idx = b["index"]
        a = after_map.get(idx)
        if not a:
            print(f"{idx:>4}  {b.get('skills','?'):<22}  {b['avg']:>6.1f}  {'n/a':>5}  {'n/a':>5}")
            continue
        delta = a["avg"] - b["avg"]
        sign = "+" if delta >= 0 else ""
        print(f"{idx:>4}  {b.get('skills','?'):<22}  {b['avg']:>6.1f}  {a['avg']:>5.1f}  {sign}{delta:.1f}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval",      default="eval_results.json",
                        help="Path to eval_results.json (default: eval_results.json)")
    parser.add_argument("--min-score", type=float, default=4.0,
                        help="Emails with avg below this are considered failures (default: 4.0)")
    parser.add_argument("--apply",     action="store_true",
                        help="Apply proposals to disk and re-run eval to measure delta")
    args = parser.parse_args()

    client = anthropic.Anthropic()

    # Step 1: Load & filter
    print(f"Loading {args.eval} …")
    records = _load_eval(args.eval)
    failing = [r for r in records if r["avg"] < args.min_score]
    print(f"  {len(records)} total records, {len(failing)} below min-score {args.min_score}")

    if not failing:
        print("All scores above threshold — nothing to improve.")
        return

    # Step 2: Load context
    all_skills = _load_all_skills()
    kb = _load_kb()
    print(f"  {len(all_skills)} skills loaded, {len(kb)} KB entries loaded")

    # Step 3: Group by skill
    by_skill: dict[str, list[dict]] = defaultdict(list)
    for r in failing:
        skill_name = r.get("skills", "unknown").split(",")[0].strip()
        by_skill[skill_name].append(r)

    # Step 4: Generate proposals per group
    all_proposals: list[dict] = []
    for skill_name, group in by_skill.items():
        skill_info = all_skills.get(skill_name)
        print(f"\nAnalysing skill '{skill_name}' ({len(group)} failing email(s)) …")
        try:
            proposals = _generate_proposals(client, skill_name, skill_info, group, kb)
            print(f"  → {len(proposals)} proposal(s)")
            all_proposals.extend(proposals)
        except Exception as exc:
            log.error("Proposal generation failed for skill '%s': %s", skill_name, exc)

    if not all_proposals:
        print("\nNo proposals generated.")
        return

    # Step 5: Write proposals
    print()
    _write_proposals(all_proposals)

    if not args.apply:
        print("\nRun with --apply to apply proposals and re-evaluate.")
        return

    # Step 6: Apply
    print("\nApplying proposals …")
    _apply_proposals(all_proposals)

    # Step 7: Re-evaluate
    print("\nRe-evaluating …")
    after = _reeval(client, failing)
    _print_delta(failing, after)

    # Merge refreshed sections back into the full records list and update files
    after_map = {r["index"]: r for r in after}
    merged = [after_map.get(r["index"], r) for r in records]
    _write_json(merged)
    _write_output(merged)


if __name__ == "__main__":
    main()
