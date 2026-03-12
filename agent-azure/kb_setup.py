"""Knowledge base setup and update for the agent-azure pipeline.

Initial setup (run once):
    python kb_setup.py
    Prints VECTOR_STORE_ID — copy into your .env file.

Called programmatically by improver.py after KB entries are added:
    from kb_setup import update_kb
    update_kb(vector_store_id)
"""
import json
import os
import pathlib
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

load_dotenv()

DATA_DIR = pathlib.Path(__file__).parent / "data"
KB_FILE = DATA_DIR / "knowledge_base.json"


def build_markdown() -> str:
    """Convert knowledge_base.json to a structured markdown document."""
    from collections import defaultdict

    with open(KB_FILE) as f:
        kb_entries: list[dict] = json.load(f)

    by_category: dict[str, list[dict]] = defaultdict(list)
    for entry in kb_entries:
        by_category[entry["category"].title()].append(entry)

    lines: list[str] = ["# Customer Support Knowledge Base\n"]
    for category, entries in sorted(by_category.items()):
        lines.append(f"\n## {category}\n")
        for entry in entries:
            lines.append(f"### {entry['topic']}")
            lines.append(f"**Q:** {entry['question']}")
            lines.append(f"**A:** {entry['answer']}")
            if entry.get("keywords"):
                lines.append(f"*Keywords: {', '.join(entry['keywords'])}*")
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


def update_kb(vector_store_id: str) -> None:
    """Re-upload the current knowledge_base.json to an existing vector store.

    Called by improver.py after new KB entries are added.
    Uploads a fresh file and attaches it, replacing the previous upload.
    """
    client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )

    kb_markdown = build_markdown()
    kb_bytes = kb_markdown.encode("utf-8")

    uploaded = client.files.upload(
        file=("knowledge_base.md", kb_bytes),
        purpose="assistants",
    )

    client.vector_store_files.create_and_poll(
        vector_store_id=vector_store_id,
        file_id=uploaded.id,
    )


if __name__ == "__main__":
    main()
