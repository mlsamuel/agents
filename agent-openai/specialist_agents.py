"""
specialist_agents.py - OpenAI Assistants specialist agent factory.

Each specialist is an OpenAI Assistant with:
  - Skill content (Markdown) as system prompt (instructions)
  - Function tools for CRM and ticketing actions — dispatched by run_with_tool_dispatch
  - file_search tool connected to the KB vector store

Public API:
    create_specialist(client, agent_key, skill_content, vector_store_id, skill_tools) -> (assistant, thread)
    run_specialist(client, assistant, thread, email, classification) -> SpecialistResult
    cleanup(client, assistant, thread) -> None
"""

import os
import re
from dataclasses import dataclass, field

from openai import OpenAI

from agent_utils import run_with_tool_dispatch
from logger import get_logger
from tools import ALL_TOOLS, TOOL_DEFINITIONS

log = get_logger(__name__)

MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")

_TICKET_RE = re.compile(r"TKT-\d+")

# Map tool name → TOOL_DEFINITIONS entry for fast lookup
_TOOL_DEF_BY_NAME = {d["function"]["name"]: d for d in TOOL_DEFINITIONS}


@dataclass
class SpecialistResult:
    agent_key: str
    skill_name: str
    reply: str
    ticket_id: str | None
    escalated: bool
    tools_called: list[str] = field(default_factory=list)
    files_searched: list[str] = field(default_factory=list)
    internal_summary: str = ""
    steps_log: list[dict] = field(default_factory=list)


def create_specialist(
    client: OpenAI,
    agent_key: str,
    skill_content: str,
    vector_store_id: str,
    skill_tools: list | None = None,
) -> tuple:
    """Create an OpenAI Assistant specialist with function tools + file_search.

    Returns (assistant, thread).
    """
    tools = []
    # Add function tools declared in the skill's frontmatter
    for tool_name in (skill_tools or []):
        if tool_name in _TOOL_DEF_BY_NAME:
            tools.append(_TOOL_DEF_BY_NAME[tool_name])
    # Always add file_search if a vector store is configured
    if vector_store_id:
        tools.append({"type": "file_search"})

    kwargs = {
        "model": MODEL,
        "name": f"{agent_key}-specialist",
        "instructions": skill_content,
        "tools": tools,
    }
    if vector_store_id:
        kwargs["tool_resources"] = {"file_search": {"vector_store_ids": [vector_store_id]}}

    assistant = client.beta.assistants.create(**kwargs)
    thread = client.beta.threads.create()
    return assistant, thread


def _send_and_run(client: OpenAI, assistant, thread, email: dict, classification: dict):
    """Post the user message and execute the run with tool dispatch. Returns the run."""
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:1500]

    user_msg = (
        f"<email>\n"
        f"  <subject>{subject}</subject>\n"
        f"  <body>{body}</body>\n"
        f"</email>\n\n"
        f"Classification: queue={classification.get('queue')}, "
        f"priority={classification.get('priority')}, "
        f"type={classification.get('type')}\n\n"
        f"Handle this email according to your workflow."
    )
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_msg,
    )

    # Build the tool_fns dict restricted to tools this assistant actually has
    tool_names = {
        t["function"]["name"]
        for t in assistant.tools
        if t.type == "function"
    }
    tool_fns = {name: fn for name, fn in ALL_TOOLS.items() if name in tool_names}

    run = run_with_tool_dispatch(client, thread.id, assistant.id, tool_fns)

    if run.status not in ("completed", "incomplete"):
        raise RuntimeError(
            f"Specialist run failed: status={run.status}, "
            f"error={getattr(run, 'last_error', None)}"
        )
    return run


def _extract_reply(client: OpenAI, thread, run) -> str:
    """Extract the assistant's reply text from the thread messages."""
    reply = ""
    if run.status in ("completed", "incomplete"):
        messages = client.beta.threads.messages.list(thread_id=thread.id, order="desc")
        for msg in messages:
            if msg.role == "assistant":
                for part in msg.content:
                    if part.type == "text":
                        text_value = part.text.value.strip()
                        # Strip file citation annotations
                        for ann in getattr(part.text, "annotations", []):
                            if hasattr(ann, "text"):
                                text_value = text_value.replace(ann.text, "")
                        reply = text_value
                break
    if not reply:
        reply = (
            "Thank you for contacting us. We have received your request and a "
            "support agent will follow up with you shortly."
        )
    return reply


def _parse_steps(client: OpenAI, thread, run) -> tuple[list[str], list[str], list[dict], bool]:
    """Parse run steps into (tools_called, files_searched, steps_log, escalated).

    Steps are returned most-recent-first; reversed for chronological order.
    """
    tools_called: list[str] = []
    files_searched: list[str] = []
    steps_log: list[dict] = []
    escalated = False

    raw_steps = list(client.beta.threads.runs.steps.list(thread_id=thread.id, run_id=run.id))
    for step_num, step in enumerate(reversed(raw_steps), start=1):
        if step.type != "tool_calls" or not step.step_details:
            continue
        for tc in step.step_details.tool_calls:
            if tc.type == "function":
                name = tc.function.name
                tools_called.append(name)
                if name == "escalate_to_human":
                    escalated = True
                steps_log.append({
                    "step": step_num,
                    "type": "function",
                    "name": name,
                    "args": tc.function.arguments,
                })
            elif tc.type == "file_search":
                results = getattr(tc.file_search, "results", None) or []
                fnames = []
                for r in results:
                    fname = getattr(r, "file_name", None)
                    if fname and fname not in files_searched:
                        files_searched.append(fname)
                    if fname and fname not in fnames:
                        fnames.append(fname)
                steps_log.append({
                    "step": step_num,
                    "type": "file_search",
                    "files": fnames,
                })

    return tools_called, files_searched, steps_log, escalated


def run_specialist(
    client: OpenAI,
    assistant,
    thread,
    email: dict,
    classification: dict,
) -> SpecialistResult:
    """Send an email to a specialist assistant and return the result."""
    agent_key = assistant.name.replace("-specialist", "")

    run = _send_and_run(client, assistant, thread, email, classification)
    reply = _extract_reply(client, thread, run)
    tools_called, files_searched, steps_log, escalated = _parse_steps(client, thread, run)

    m = _TICKET_RE.search(reply)
    return SpecialistResult(
        agent_key=agent_key,
        skill_name=assistant.name,
        reply=reply,
        ticket_id=m.group(0) if m else None,
        escalated=escalated,
        tools_called=tools_called,
        files_searched=files_searched,
        internal_summary=reply.split(".")[0].strip(),
        steps_log=steps_log,
    )


def cleanup(client: OpenAI, assistant, thread) -> None:
    """Delete the assistant and thread after use."""
    try:
        client.beta.threads.delete(thread.id)
    except Exception:
        pass
    try:
        client.beta.assistants.delete(assistant.id)
    except Exception:
        pass
