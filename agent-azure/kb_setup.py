"""One-time script to upload the customer support knowledge base to an Azure vector store.

Run once before kb_agent.py:
    python kb_setup.py

Copy the printed VECTOR_STORE_ID into your .env file.
"""
import json
import os
import pathlib
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

load_dotenv()

DATA_DIR = pathlib.Path(__file__).parent.parent / "agent-langgraph" / "data"
KB_FILE = DATA_DIR / "knowledge_base.json"
GUIDELINES_FILE = DATA_DIR / "agent_guidelines.json"


def build_markdown() -> str:
    """Convert KB JSON files to a single structured markdown document."""
    lines: list[str] = []

    lines.append("# Customer Support Knowledge Base\n")

    with open(KB_FILE) as f:
        kb_entries: list[dict] = json.load(f)

    # Group by category
    from collections import defaultdict
    by_category: dict[str, list[dict]] = defaultdict(list)
    for entry in kb_entries:
        by_category[entry["category"].title()].append(entry)

    for category, entries in sorted(by_category.items()):
        lines.append(f"\n## {category}\n")
        for entry in entries:
            lines.append(f"### {entry['topic']}")
            lines.append(f"**Q:** {entry['question']}")
            lines.append(f"**A:** {entry['answer']}")
            if entry.get("keywords"):
                lines.append(f"*Keywords: {', '.join(entry['keywords'])}*")
            lines.append("")

    if GUIDELINES_FILE.exists():
        with open(GUIDELINES_FILE) as f:
            guidelines: list[dict] = json.load(f)

        lines.append("\n# Agent Guidelines\n")
        for g in guidelines:
            lines.append(f"### {g['topic']} ({g['category']})")
            lines.append(f"**Trigger:** {g['trigger']}")
            lines.append(f"**Instruction:** {g['instruction']}")
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )

    print("Building knowledge base markdown...")
    kb_markdown = build_markdown()
    kb_bytes = kb_markdown.encode("utf-8")
    print(f"  {len(kb_bytes):,} bytes, "
          f"{kb_markdown.count('###')} entries")

    print("Creating vector store...")
    vector_store = client.vector_stores.create_and_poll(
        name="customer-support-kb"
    )
    print(f"  Vector store: {vector_store.id}")

    print("Uploading knowledge base file...")
    uploaded = client.files.upload(
        file=("knowledge_base.md", kb_bytes),
        purpose="assistants",
    )
    print(f"  File ID: {uploaded.id}")

    print("Attaching file to vector store...")
    vs_file = client.vector_store_files.create_and_poll(
        vector_store_id=vector_store.id,
        file_id=uploaded.id,
    )
    print(f"  Status: {vs_file.status}")

    print("\n✓ Done. Add this to your .env:")
    print(f"\n  VECTOR_STORE_ID={vector_store.id}\n")


if __name__ == "__main__":
    main()
