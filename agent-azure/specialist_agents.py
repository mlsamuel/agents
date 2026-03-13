"""
specialist_agents.py - Foundry specialist agent factory.

Each specialist is a Foundry agent with:
  - Skill content (Markdown) as system prompt
  - FunctionTool set appropriate for their domain (via enable_auto_function_calls)
  - FileSearchTool connected to the KB vector store

Public API:
    create_specialist(client, agent_key, skill_content, vector_store_id, skill_tools) -> (agent, thread)
    run_specialist(client, agent, thread, email, classification) -> SpecialistResult
    cleanup(client, agent, thread) -> None
"""

import os
import re
from dataclasses import dataclass, field

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool, FileSearchTool, FunctionTool, ToolSet

from agent_utils import run_with_retry
from logger import get_logger
from tools import ALL_TOOLS

log = get_logger(__name__)

MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")

_TICKET_RE = re.compile(r"TKT-\d+")


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
    client: AgentsClient,
    agent_key: str,
    skill_content: str,
    vector_store_id: str,
    skill_tools: list | None = None,
) -> tuple:
    """Create a Foundry specialist agent with FunctionTools + FileSearch.

    The client must already have enable_auto_function_calls configured
    (done by make_client) so that create_and_process can dispatch calls.

    Returns (agent, thread).
    """
    toolset = ToolSet()
    tool_fns = {ALL_TOOLS[t] for t in (skill_tools or []) if t in ALL_TOOLS}
    if tool_fns:
        toolset.add(FunctionTool(functions=tool_fns))
    if vector_store_id:
        toolset.add(FileSearchTool(vector_store_ids=[vector_store_id]))
    if skill_tools and "code_interpreter" in skill_tools:
        toolset.add(CodeInterpreterTool())

    agent = client.create_agent(
        model=MODEL,
        name=f"{agent_key}-specialist",
        instructions=skill_content,
        toolset=toolset,
    )
    thread = client.threads.create()
    return agent, thread


def _send_and_run(client, agent, thread, email: dict, classification: dict):
    """Post the user message and execute the agent run. Returns the completed run object."""
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
    client.messages.create(thread_id=thread.id, role="user", content=user_msg)

    run = run_with_retry(client, thread.id, agent.id)

    if run.status == "incomplete":
        reason = getattr(getattr(run, "incomplete_details", None), "reason", "unknown")
        if reason != "content_filter":
            raise RuntimeError(f"Specialist run failed: status={run.status}, reason={reason}")
    elif run.status != "completed":
        raise RuntimeError(
            f"Specialist run failed: status={run.status}, "
            f"error={getattr(run, 'last_error', None)}"
        )

    return run


def _extract_reply(client, thread, run) -> str:
    """Extract the assistant's reply text from the thread messages."""
    reply = ""
    if run.status == "completed":
        for msg in client.messages.list(thread_id=thread.id):
            if msg.role == "assistant":
                for part in msg.content:
                    if hasattr(part, "text"):
                        reply = part.text.value.strip()
                        if hasattr(part.text, "annotations"):
                            for ann in part.text.annotations:
                                if hasattr(ann, "text"):
                                    reply = reply.replace(ann.text, "")
                        break
                break
    if not reply:
        reply = (
            "Thank you for contacting us. We have received your request and a "
            "support agent will follow up with you shortly."
        )
    return reply


def _parse_steps(client, thread, run) -> tuple[list[str], list[str], list[dict], bool]:
    """Parse run steps into (tools_called, files_searched, steps_log, escalated).

    run_steps.list returns most-recent-first; reverse for chronological order.
    """
    tools_called: list[str] = []
    files_searched: list[str] = []
    steps_log: list[dict] = []
    escalated = False

    raw_steps = list(client.run_steps.list(thread_id=thread.id, run_id=run.id))
    for step_num, step in enumerate(reversed(raw_steps), start=1):
        if step.type != "tool_calls" or not step.step_details:
            continue
        for tc in step.step_details.tool_calls:
            if hasattr(tc, "function"):
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
            elif tc.type == "code_interpreter":
                tools_called.append("code_interpreter")
                ci = getattr(tc, "code_interpreter", None)
                code = getattr(ci, "input", "") if ci else ""
                outputs = getattr(ci, "outputs", []) if ci else []
                out_parts = []
                for o in outputs:
                    otype = getattr(o, "type", "unknown")
                    if otype == "logs":
                        text = getattr(o, "logs", "")
                    else:
                        text = (getattr(o, "logs", None)
                                or getattr(o, "text", None)
                                or f"[{otype} output — no text attribute]")
                    if text:
                        out_parts.append(text)
                steps_log.append({
                    "step": step_num,
                    "type": "code_interpreter",
                    "code": code,
                    "output": "\n".join(out_parts),
                    "output_count": len(outputs),
                })
            elif tc.type == "file_search" and hasattr(tc, "file_search"):
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
    client: AgentsClient,
    agent,
    thread,
    email: dict,
    classification: dict,
) -> SpecialistResult:
    """Send an email to a specialist agent and return the result."""
    agent_key = agent.name.replace("-specialist", "")

    run = _send_and_run(client, agent, thread, email, classification)
    reply = _extract_reply(client, thread, run)
    tools_called, files_searched, steps_log, escalated = _parse_steps(client, thread, run)

    m = _TICKET_RE.search(reply)
    return SpecialistResult(
        agent_key=agent_key,
        skill_name=agent.name,
        reply=reply,
        ticket_id=m.group(0) if m else None,
        escalated=escalated,
        tools_called=tools_called,
        files_searched=files_searched,
        internal_summary=reply.split(".")[0].strip(),
        steps_log=steps_log,
    )


def cleanup(client: AgentsClient, agent, thread) -> None:
    """Delete the agent and thread after use."""
    try:
        client.threads.delete(thread.id)
    except Exception:
        pass
    try:
        client.delete_agent(agent.id)
    except Exception:
        pass
