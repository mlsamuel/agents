"""Knowledge base and guidelines setup for the agent-azure pipeline.

Run once to create the vector store, or again to refresh it:
    python kb_setup.py

If VECTOR_STORE_ID is already in .env the existing store is updated in place
(old KB/guidelines files are deleted before uploading fresh ones).
If it is absent a new store is created and the ID is printed.

Called programmatically by improver.py:
    from kb_setup import update_kb, update_guidelines
"""
import json
import os
import pathlib
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

load_dotenv()

DATA_DIR        = pathlib.Path(__file__).parent / "data"
KB_FILE         = DATA_DIR / "knowledge_base.json"
GUIDELINES_FILE = DATA_DIR / "agent_guidelines.json"

_GUIDELINES_FILENAME = "agent_guidelines.md"


# ── Markdown builders ─────────────────────────────────────────────────────────

def _kb_filename(category: str) -> str:
    """Return the vector store filename for a KB category, e.g. 'kb_billing.md'."""
    return f"kb_{category.lower().replace(' ', '_')}.md"


def build_category_markdowns() -> dict[str, str]:
    """Convert knowledge_base.json to one markdown string per category.

    Returns {category_key: markdown_content} where category_key is the raw
    lowercase category value from the JSON (used for filenames and lookups).
    """
    from collections import defaultdict
    kb_entries: list[dict] = json.loads(KB_FILE.read_text(encoding="utf-8"))

    by_category: dict[str, list[dict]] = defaultdict(list)
    for entry in kb_entries:
        by_category[entry["category"]].append(entry)

    result = {}
    for category, entries in sorted(by_category.items()):
        lines: list[str] = [f"# Customer Support Knowledge Base — {category.title()}\n"]
        for entry in entries:
            lines.append(f"### {entry['topic']}")
            lines.append(f"**Q:** {entry['question']}")
            lines.append(f"**A:** {entry['answer']}")
            if entry.get("keywords"):
                lines.append(f"*Keywords: {', '.join(entry['keywords'])}*")
            lines.append("")
        result[category] = "\n".join(lines)
    return result


def build_guidelines_markdown() -> str:
    """Convert agent_guidelines.json to a structured markdown document."""
    from collections import defaultdict
    if not GUIDELINES_FILE.exists():
        return ""
    guidelines: list[dict] = json.loads(GUIDELINES_FILE.read_text(encoding="utf-8"))
    if not guidelines:
        return ""

    by_category: dict[str, list[dict]] = defaultdict(list)
    for g in guidelines:
        by_category[g.get("category", "general").title()].append(g)

    lines: list[str] = ["# Agent Behaviour Guidelines\n"]
    for category, entries in sorted(by_category.items()):
        lines.append(f"\n## {category}\n")
        for g in entries:
            lines.append(f"### {g.get('topic', 'Guideline')}")
            if g.get("trigger"):
                lines.append(f"**When:** {g['trigger']}")
            if g.get("instruction"):
                lines.append(f"**Do:** {g['instruction']}")
            if g.get("keywords"):
                lines.append(f"*Keywords: {', '.join(g['keywords'])}*")
            lines.append("")
    return "\n".join(lines)


# ── Upload helpers ────────────────────────────────────────────────────────────

def _replace_file(client, content: str, filename: str, vector_store_id: str) -> None:
    """Upload a new version of filename to the vector store, removing the old one.

    Listing vector store files and matching by the underlying file's filename
    lets us avoid accumulating stale versions on repeated uploads.
    """
    # Remove any existing attachment with the same filename
    for vsf in client.vector_store_files.list(vector_store_id=vector_store_id):
        try:
            meta = client.files.get(file_id=vsf.id)
            if meta.filename == filename:
                client.vector_store_files.delete(
                    vector_store_id=vector_store_id, file_id=vsf.id
                )
                client.files.delete(file_id=vsf.id)
        except Exception:
            pass  # best-effort cleanup

    uploaded = client.files.upload(
        file=(filename, content.encode("utf-8")),
        purpose="assistants",
    )
    client.vector_store_files.create_and_poll(
        vector_store_id=vector_store_id,
        file_id=uploaded.id,
    )


def _make_client() -> AgentsClient:
    return AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )


# ── Public update API (called by improver.py) ─────────────────────────────────

def update_kb(vector_store_id: str) -> None:
    """Re-upload all KB category files, replacing previous versions."""
    client = _make_client()
    for category, content in build_category_markdowns().items():
        _replace_file(client, content, _kb_filename(category), vector_store_id)


def update_kb_category(vector_store_id: str, category: str) -> None:
    """Re-upload only the KB file for one category. Called by improver after adding an entry."""
    markdowns = build_category_markdowns()
    content = markdowns.get(category)
    if not content:
        # Category not found — fall back to full rebuild
        update_kb(vector_store_id)
        return
    _replace_file(_make_client(), content, _kb_filename(category), vector_store_id)


def update_guidelines(vector_store_id: str) -> None:
    """Re-upload current agent_guidelines.json, replacing the previous version.

    No-op if the guidelines file is empty.
    """
    content = build_guidelines_markdown()
    if not content:
        return
    _replace_file(_make_client(), content, _GUIDELINES_FILENAME, vector_store_id)


# ── main: create-or-update ────────────────────────────────────────────────────

def main() -> None:
    client = _make_client()
    vector_store_id = os.environ.get("VECTOR_STORE_ID", "").strip()

    if vector_store_id:
        # Verify the store still exists
        try:
            client.vector_stores.get(vector_store_id=vector_store_id)
            print(f"Reusing existing vector store: {vector_store_id}")
        except Exception:
            print(f"Vector store {vector_store_id} not found — creating a new one.")
            vector_store_id = ""

    if not vector_store_id:
        vs = client.vector_stores.create_and_poll(name="customer-support-kb")
        vector_store_id = vs.id
        print(f"Created vector store: {vector_store_id}")
        print(f"\n  Add to your .env:  VECTOR_STORE_ID={vector_store_id}\n")

    print("Uploading knowledge base (per category)...")
    category_mds = build_category_markdowns()
    total_entries = 0
    for category, content in category_mds.items():
        _replace_file(client, content, _kb_filename(category), vector_store_id)
        n = content.count("###")
        total_entries += n
        print(f"  {_kb_filename(category)}: {n} entries")
    print(f"  {total_entries} total entries — done")

    guidelines_md = build_guidelines_markdown()
    if guidelines_md:
        print("Uploading guidelines...")
        _replace_file(client, guidelines_md, _GUIDELINES_FILENAME, vector_store_id)
        print(f"  {guidelines_md.count('###')} entries — done")
    else:
        print("Guidelines file empty — skipping")

    print("\n✓ Vector store is up to date.")


if __name__ == "__main__":
    main()
