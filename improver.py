"""
improver.py - Eval-driven skill/KB improvement helpers.

Public API:
  load_kb()
  load_all_skills()
  generate_proposals(client, skill_name, skill_info, records, kb) → list[dict]
  apply_proposals(all_proposals)
  reeval(client, failing) → list[dict]
  print_delta(before, after)
"""

import asyncio
import json
from pathlib import Path
import yaml

from client import Client
from email_stream import email_stream
from classifier import classify
from orchestrator_agent import orchestrate
from evaluator import judge
from logger import get_logger
import kb
import skills as skills_db

log = get_logger(__name__)

KB_PATH      = Path(__file__).parent / "data" / "knowledge_base.json"
IMPROVE_MODEL = "claude-sonnet-4-6"
MERGE_MODEL   = "claude-haiku-4-5-20251001"
MERGE_SYSTEM  = """\
Merge two knowledge base entries on the same topic into one.
Keep the existing entry's id and category unchanged.
Combine the question and answer to preserve all unique factual content from both.
Respond with JSON only, no markdown wrapper, matching this exact schema:
{"id": "...", "category": "...", "topic": "...", "question": "...", "answer": "...", "keywords": [...]}
"""
KB_SIMILARITY_THRESHOLD = 0.90

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

## Decision rules — apply in order

### kb_entry (ALWAYS do this when applicable)
Propose a kb_entry whenever the ground truth reply contains specific factual information
that the agent's reply was missing — e.g. policies, pricing, product details, procedures,
deadlines, supported platforms, contact details. Extract the fact directly from the ground
truth; do not invent or generalise. You MUST propose a kb_entry any time the generated
reply omits concrete information that is present in the ground truth.

### skill_edit (ONLY when the eval comment identifies a process issue)
Propose a skill_edit ONLY when the eval comment explicitly describes a workflow or
process problem — e.g. "agent escalated instead of asking a question first", "agent
should have looked up ticket history before replying", "agent created a ticket when it
should have asked for clarification". Do NOT propose a skill_edit just because the reply
lacked information; that is a kb_entry problem, not a skill problem.

### new_skill (rare)
Propose a new_skill only when the email type is entirely unhandled by any existing skill.

## Output rules
- Only propose changes directly evidenced by the eval comments and ground truth
- For skill_edit and new_skill: provide the COMPLETE .md file content (full rewrite).
  Preserve all existing security notes, frontmatter fields, and format rules.
- For kb_entry: derive the answer text verbatim or near-verbatim from the ground truth.
  Do NOT include an "id" field — IDs are assigned automatically by the database.
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

def load_all_skills() -> dict[str, dict]:
    """Return {skill_name: {queue, types, tools, content}} for all active skills."""
    return skills_db.load_all_sync()


def load_kb() -> list[dict]:
    with open(KB_PATH) as f:
        return json.load(f)


def _kb_for_category(kb: list[dict], category: str) -> list[dict]:
    return [e for e in kb if e.get("category") == category]



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


def generate_proposals(
    client: Client,
    skill_name: str,
    skill_info: dict | None,
    records: list[dict],
    kb: list[dict],
) -> list[dict]:
    """Call Claude to generate improvement proposals for one skill group."""
    category = QUEUE_TO_KEY.get(records[0]["queue"], "general")
    kb_entries = _kb_for_category(kb, category)

    if skill_info:
        fm = (
            f"---\n"
            f"name: {skill_name}\n"
            f"queue: {skill_info['queue']}\n"
            f"types: {skill_info['types']}\n"
            f"tools: {skill_info['tools']}\n"
            f"---\n\n"
        )
        skill_content = fm + skill_info["content"]
        skill_file    = f"skills/{skill_info['queue']}/{skill_name}.md"
    else:
        skill_content = "(skill not found)"
        skill_file    = f"skills/{category}/{skill_name}.md"

    kb_summary = [{"id": e["id"], "topic": e["topic"], "question": e["question"]}
                  for e in kb_entries]

    user_msg = (
        f"## Skill file: {skill_file}\n\n"
        f"```\n{skill_content}\n```\n\n"
        f"## Failing examples (avg < threshold)\n\n"
        f"{_build_examples_text(records)}\n\n"
        f"## Existing knowledge base entries (category: {category}) — id, topic, question only\n\n"
        f"```json\n{json.dumps(kb_summary, indent=2)}\n```\n\n"
        f"Propose improvements. Remember: respond with JSON only."
    )

    response = client.messages.create(
        model=IMPROVE_MODEL,
        max_tokens=16000,
        system=IMPROVE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"generate_proposals response was truncated (stop_reason=max_tokens, "
            f"{response.usage.output_tokens} tokens used). Increase max_tokens or reduce input."
        )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:]       # drop opening ```json line
        if raw.endswith("```"):
            raw = raw[:raw.rindex("```")].rstrip()  # drop closing ```
    data = json.loads(raw)
    return data.get("proposals", [])


# ── Apply ────────────────────────────────────────────────────────────────────

async def _merge_kb_entries(client: Client, existing: dict, proposed: dict) -> dict:
    """Ask the LLM to merge two semantically similar KB entries into one."""
    user_msg = (
        f"Existing entry:\n{json.dumps(existing, indent=2)}\n\n"
        f"Proposed entry:\n{json.dumps(proposed, indent=2)}\n\n"
        f"Merge these. Keep id={existing['id']!r} and category={existing['category']!r}."
    )
    response = client.messages.create(
        model=MERGE_MODEL,
        max_tokens=1024,
        system=MERGE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:]
        if raw.endswith("```"):
            raw = raw[:raw.rindex("```")].rstrip()
    merged = json.loads(raw)
    merged["category"] = existing["category"]  # enforce — model must not change category
    merged["topic"]    = existing["topic"]      # enforce — upsert_version matches on topic
    return merged

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter and body. Returns ({}, text) if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


async def _apply_proposals_async(client: Client, all_proposals: list[dict]) -> None:
    for p in all_proposals:
        ptype = p["type"]
        if ptype in ("skill_edit", "new_skill"):
            meta, body = _parse_frontmatter(p["new_content"])
            name  = meta.get("name", "unknown")
            queue = meta.get("queue", "")
            types = meta.get("types", [])
            tools = meta.get("tools", [])
            try:
                if ptype == "skill_edit":
                    ver = await skills_db.upsert_version(name, queue, types, tools, body)
                    print(f"  Skill updated: {name} → v{ver}")
                else:
                    await skills_db.insert_new(name, queue, types, tools, body)
                    print(f"  New skill created: {name} v1")
            except Exception as exc:
                log.error("Skill DB write failed for '%s': %s", name, exc)
                raise

        elif ptype == "kb_entry":
            entry = p["entry"]
            query = entry.get("question") or entry.get("topic", "")
            matches = await kb.search(query, top_k=1)
            if matches and matches[0]["score"] >= KB_SIMILARITY_THRESHOLD:
                existing = matches[0]
                print(f"  KB merge: '{entry.get('topic')}' ~ '{existing['topic']}' "
                      f"(score={existing['score']})")
                try:
                    merged = await _merge_kb_entries(client, existing, entry)
                    new_id = await kb.upsert_version(merged)
                    print(f"  KB entry merged into: {existing['id']} → v(new id={new_id}) — {merged.get('topic', '')}")
                except Exception as exc:
                    log.warning("KB merge failed: %s — skipping entry", exc)
            else:
                try:
                    new_id = await kb.insert(entry)
                    print(f"  KB entry added: id={new_id} — {entry.get('topic', '')}")
                except Exception as exc:
                    log.warning("KB DB insert failed: %s — skipping", exc)


async def apply_proposals(client: Client, all_proposals: list[dict]) -> None:
    await _apply_proposals_async(client, all_proposals)


# ── Re-evaluate ──────────────────────────────────────────────────────────────

def reeval(client: Client, failing: list[dict]) -> list[dict]:
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


def print_delta(before: list[dict], after: list[dict]) -> None:
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


