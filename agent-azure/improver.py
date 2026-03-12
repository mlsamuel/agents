"""
improver.py - Eval-driven skill/KB/guideline improvement.

Generates proposals from failing emails and applies them:
  - skill_edit    → write new version of skill Markdown file
  - kb_entry      → add to knowledge_base.json + re-upload to Azure vector store
  - agent_guideline → add to agent_guidelines.json
  - new_skill     → write new skill Markdown file

Public API:
    generate_proposals(client, skill_name, skill_info, record) -> list[dict]
    apply_proposals(client, proposals, vector_store_id)        -> None
"""

import json
import os
from pathlib import Path

from azure.ai.agents import AgentsClient

import skills as skills_mod
import store

IMPROVE_MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")
MERGE_MODEL   = os.environ.get("FAST_MODEL", "gpt-4o-mini")

KB_SIMILARITY_THRESHOLD_TOPIC = True  # use topic-based dedup (no local embeddings)

IMPROVE_SYSTEM = """\
You are an expert at improving customer support AI agent skills.

You will receive:
1. A skill .md file (full content) used by an agent to handle support emails
2. A failing example with scores, eval comment, ground truth reply, and generated reply

## Content types — choose exactly the right type for each proposal

### kb_entry — customer-facing answer
Propose a kb_entry when the ground truth delivers a direct, complete factual answer the
customer can act on immediately: a policy, deadline, price, product feature, or procedure
fully described in the reply itself.

The `answer` must be customer-facing prose — as if appearing on a help page. It must NOT
contain agent-perspective phrases ("request their", "ask the customer", "you must first").
If the answer describes what the agent should do — not just facts to relay to the customer
— use agent_guideline instead.

Never propose a kb_entry for transient or time-sensitive information: outage
notifications, current system status, maintenance windows, or any content that describes
a temporary state. Use skill_edit to fix outage handling behaviour instead.

### skill_edit — wrong workflow or action
Propose a skill_edit when the eval comment identifies a process error: wrong action taken,
wrong tool used, wrong order of steps, or a decision rule being applied in the wrong order.

skill_edit takes priority over agent_guideline when the failure is caused by the skill's
own workflow steps conflicting or executing in the wrong order.

### agent_guideline — agent behaviour pattern
Propose an agent_guideline when the ground truth shows the agent following a specific
behavioural pattern not already covered by the skill's workflow steps. This covers:
- Information collection: asking for prerequisites before acting
- Workflow exceptions: taking a different action when a condition is met
- Decision rules: choosing between two actions based on context

Do NOT use agent_guideline if the skill already has (or should have) a step covering
this behaviour — fix the skill with skill_edit instead.

### new_skill (rare)
Propose a new_skill only when the email type is entirely unhandled by any existing skill.

## Output rules
- Only propose changes directly evidenced by the eval comment and ground truth
- For skill_edit and new_skill: provide the COMPLETE .md file content (full rewrite).
  Preserve all existing security notes, frontmatter fields, and format rules.
- Do NOT include an "id" field in kb_entry or agent_guideline.
- Respond with valid JSON only, no markdown wrapper:
{
  "proposals": [
    {
      "type": "skill_edit",
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
      "type": "agent_guideline",
      "rationale": "one sentence explaining why",
      "entry": {
        "category": "general",
        "topic": "...",
        "trigger": "...",
        "instruction": "...",
        "keywords": ["..."]
      }
    },
    {
      "type": "new_skill",
      "rationale": "one sentence explaining why",
      "new_content": "--- full .md content here ---"
    }
  ]
}"""

MERGE_KB_SYSTEM = """\
Merge two knowledge base entries on the same topic into one.
Keep the existing entry's category unchanged.
Combine the question and answer to preserve all unique factual content from both.
Respond with JSON only, no markdown:
{"category": "...", "topic": "...", "question": "...", "answer": "...", "keywords": [...]}"""

MERGE_GUIDELINE_SYSTEM = """\
Merge two agent guideline entries on the same topic into one.
Keep the existing entry's category unchanged.
Combine the trigger and instruction to preserve all unique content from both.
Respond with JSON only, no markdown:
{"category": "...", "topic": "...", "trigger": "...", "instruction": "...", "keywords": [...]}"""


def _call_agent(client: AgentsClient, system: str, user_msg: str, model: str = IMPROVE_MODEL) -> str:
    """Create a single-turn Foundry agent call and return the text response."""
    agent = client.agents.create_agent(model=model, name="improver", instructions=system)
    thread = client.agents.threads.create()
    try:
        client.agents.messages.create(thread_id=thread.id, role="user", content=user_msg)
        run = client.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        if run.status != "completed":
            raise RuntimeError(f"Improver agent failed: {run.status}")
        for msg in client.agents.messages.list(thread_id=thread.id):
            if msg.role == "assistant":
                for part in msg.content:
                    if hasattr(part, "text"):
                        return part.text.value.strip()
    finally:
        client.agents.threads.delete(thread.id)
        client.agents.delete_agent(agent.id)
    return ""


def _strip_fences(raw: str) -> str:
    if raw.startswith("```"):
        raw = raw[raw.index("\n") + 1:]
        if raw.endswith("```"):
            raw = raw[:raw.rindex("```")].rstrip()
    return raw


def generate_proposals(
    client: AgentsClient,
    skill_name: str,
    skill_info: dict | None,
    record: dict,
) -> list[dict]:
    """Generate improvement proposals for a failing email record."""
    if skill_info:
        fm = (
            f"---\n"
            f"name: {skill_name}\n"
            f"agent: {skill_info.get('agent', '')}\n"
            f"types: {skill_info.get('types', [])}\n"
            f"tools: {skill_info.get('tools', [])}\n"
            f"---\n\n"
        )
        skill_content = fm + skill_info["content"]
    else:
        skill_content = "(skill not found)"

    s = record["score"]
    example_text = (
        f"Subject: {record['subject']}\n"
        f"Scores: action={s['action']}/5  completeness={s['completeness']}/5  "
        f"tone={s['tone']}/5  avg={record['avg']:.1f}\n"
        f"Eval comment: {s['comment']}\n\n"
        f"Ground truth reply:\n{record['ground_truth'][:800]}\n\n"
        f"Generated reply:\n{record['generated'][:800]}"
    )

    user_msg = (
        f"## Skill: {skill_name}\n\n"
        f"```\n{skill_content}\n```\n\n"
        f"## Failing example\n\n{example_text}\n\n"
        f"Propose improvements. Respond with JSON only."
    )

    raw = _call_agent(client, IMPROVE_SYSTEM, user_msg, model=IMPROVE_MODEL)
    raw = _strip_fences(raw)
    data = json.loads(raw)
    return data.get("proposals", [])


# ── Apply ─────────────────────────────────────────────────────────────────────

def _merge_kb_entries(client: AgentsClient, existing: dict, proposed: dict) -> dict:
    user_msg = (
        f"Existing entry:\n{json.dumps(existing, indent=2)}\n\n"
        f"Proposed entry:\n{json.dumps(proposed, indent=2)}\n\n"
        f"Merge these. Keep category={existing['category']!r}."
    )
    raw = _call_agent(client, MERGE_KB_SYSTEM, user_msg, model=MERGE_MODEL)
    raw = _strip_fences(raw)
    merged = json.loads(raw)
    merged["category"] = existing["category"]
    merged["topic"] = existing["topic"]
    return merged


def _merge_guideline_entries(client: AgentsClient, existing: dict, proposed: dict) -> dict:
    user_msg = (
        f"Existing entry:\n{json.dumps(existing, indent=2)}\n\n"
        f"Proposed entry:\n{json.dumps(proposed, indent=2)}\n\n"
        f"Merge these. Keep category={existing['category']!r}."
    )
    raw = _call_agent(client, MERGE_GUIDELINE_SYSTEM, user_msg, model=MERGE_MODEL)
    raw = _strip_fences(raw)
    merged = json.loads(raw)
    merged["category"] = existing["category"]
    merged["topic"] = existing["topic"]
    return merged


def apply_proposals(
    client: AgentsClient,
    proposals: list[dict],
    vector_store_id: str,
) -> None:
    """Apply improvement proposals to skills, KB, and guidelines."""
    from kb_setup import update_kb  # import here to avoid circular deps

    kb_path = Path(__file__).parent / "data" / "knowledge_base.json"

    for p in proposals:
        ptype = p["type"]

        if ptype in ("skill_edit", "new_skill"):
            meta, body = skills_mod.parse_frontmatter(p["new_content"])
            agent_key = meta.get("agent", "general")
            skill_name = meta.get("name", "unknown")
            skills_dir = Path(__file__).parent / "data" / "skills" / agent_key
            skills_dir.mkdir(parents=True, exist_ok=True)
            skill_path = skills_dir / f"{skill_name}.md"

            if ptype == "skill_edit" and skill_path.exists():
                new_ver = skills_mod.upsert_version(str(skill_path), p["new_content"])
                print(f"  Skill updated: {skill_name} → v{new_ver}")
            else:
                skill_path.write_text(p["new_content"], encoding="utf-8")
                print(f"  New skill created: {skill_name}")

        elif ptype == "kb_entry":
            entry = p["entry"]
            # Load existing KB
            kb_entries: list[dict] = json.loads(kb_path.read_text(encoding="utf-8"))

            # Topic-based dedup: merge if same topic exists
            existing = next(
                (e for e in kb_entries if e.get("topic", "").lower() == entry.get("topic", "").lower()),
                None,
            )
            if existing:
                print(f"  KB merge: '{entry.get('topic')}' ~ '{existing['topic']}'")
                try:
                    merged = _merge_kb_entries(client, existing, entry)
                    existing.update(merged)
                    print(f"  KB entry merged: {merged.get('topic', '')}")
                except Exception as exc:
                    print(f"  KB merge failed: {exc} — skipping")
                    continue
            else:
                new_id = max((e.get("id", 0) for e in kb_entries), default=0) + 1
                entry["id"] = new_id
                kb_entries.append(entry)
                print(f"  KB entry added: id={new_id} — {entry.get('topic', '')}")

            # Save and re-upload
            kb_path.write_text(json.dumps(kb_entries, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                update_kb(vector_store_id)
                print(f"  KB re-uploaded to vector store")
            except Exception as exc:
                print(f"  KB upload failed: {exc}")

        elif ptype == "agent_guideline":
            entry = p["entry"]
            guidelines = store.load_guidelines()

            # Topic-based dedup
            existing = next(
                (g for g in guidelines if g.get("topic", "").lower() == entry.get("topic", "").lower()),
                None,
            )
            if existing:
                print(f"  Guideline merge: '{entry.get('topic')}' ~ '{existing['topic']}'")
                try:
                    merged = _merge_guideline_entries(client, existing, entry)
                    existing.update(merged)
                    store.save_guidelines(guidelines)
                    print(f"  Guideline merged: {merged.get('topic', '')}")
                except Exception as exc:
                    print(f"  Guideline merge failed: {exc} — skipping")
            else:
                store.add_guideline(entry)
                print(f"  Guideline added: {entry.get('topic', '')}")
