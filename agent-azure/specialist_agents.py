"""
specialist_agents.py - Foundry specialist agent factory.

Each specialist is a Foundry agent with:
  - Skill content (Markdown) as system prompt + agent guidelines appended
  - FunctionTool set appropriate for their domain (via enable_auto_function_calls)
  - FileSearchTool connected to the KB vector store

Design note: each specialist gets its own AgentsClient so that
enable_auto_function_calls (which sets global state on the client) is
safe to use in parallel ThreadPoolExecutor workers.

Public API:
    create_specialist(client, agent_key, skill_content,
                      vector_store_id, guidelines_text)
        -> (agent, thread)

    run_specialist(client, agent, thread, email, classification)
        -> SpecialistResult

    cleanup(client, agent, thread) -> None

    make_client(endpoint, credential) -> AgentsClient
        Convenience: build a fresh client with auto-function-calls for agent_key.
"""

import os
import re
import time
from dataclasses import dataclass, field

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool, FileSearchTool, FunctionTool, ToolSet
from azure.core.credentials import TokenCredential

from tools import SPECIALIST_TOOLS

MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")

CODE_INTERPRETER_AGENTS = {"billing", "technical_support"}

_TICKET_RE = re.compile(r"TKT-\d+")


def _run_with_retry(client, thread_id, agent_id, attempts=3):
    """Run create_and_process with retries on transient failures (e.g. timeouts)."""
    for i in range(attempts):
        try:
            return client.runs.create_and_process(thread_id=thread_id, agent_id=agent_id)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2 ** i)


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


def make_client(endpoint: str, credential: TokenCredential, agent_key: str) -> AgentsClient:
    """Create a fresh AgentsClient pre-configured for agent_key's function tools.

    Using a per-specialist client lets us call enable_auto_function_calls safely
    in parallel threads without clobbering each other's registered functions.
    """
    client = AgentsClient(endpoint=endpoint, credential=credential)
    tool_fns = SPECIALIST_TOOLS.get(agent_key, set())
    if tool_fns:
        client.enable_auto_function_calls(tool_fns)
    return client


def create_specialist(
    client: AgentsClient,
    agent_key: str,
    skill_content: str,
    vector_store_id: str,
) -> tuple:
    """Create a Foundry specialist agent with FunctionTools + FileSearch.

    The client must already have enable_auto_function_calls configured
    (done by make_client) so that create_and_process can dispatch calls.
    Guidelines are retrieved at runtime via FileSearch (same vector store as KB).

    Returns (agent, thread).
    """
    system_prompt = skill_content

    toolset = ToolSet()
    tool_fns = SPECIALIST_TOOLS.get(agent_key, set())
    if tool_fns:
        toolset.add(FunctionTool(functions=tool_fns))
    if vector_store_id:
        toolset.add(FileSearchTool(vector_store_ids=[vector_store_id]))
    if agent_key in CODE_INTERPRETER_AGENTS:
        toolset.add(CodeInterpreterTool())

    agent = client.create_agent(
        model=MODEL,
        name=f"{agent_key}-specialist",
        instructions=system_prompt,
        toolset=toolset,
    )
    thread = client.threads.create()
    return agent, thread


def run_specialist(
    client: AgentsClient,
    agent,
    thread,
    email: dict,
    classification: dict,
) -> SpecialistResult:
    """Send an email to a specialist agent and return the result."""
    agent_key = agent.name.replace("-specialist", "")
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

    # create_and_process handles the tool-call loop automatically because
    # make_client registered our functions via enable_auto_function_calls.
    run = _run_with_retry(client, thread.id, agent.id)

    if run.status == "incomplete":
        reason = getattr(getattr(run, "incomplete_details", None), "reason", "unknown")
        if reason != "content_filter":
            raise RuntimeError(
                f"Specialist run failed: status={run.status}, reason={reason}"
            )
        # content_filter: fall through with empty reply; fallback applied below
    elif run.status != "completed":
        raise RuntimeError(
            f"Specialist run failed: status={run.status}, "
            f"error={getattr(run, 'last_error', None)}"
        )

    # Extract reply
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

    # Extract ticket_id from reply text (skill instructions require it there)
    m = _TICKET_RE.search(reply)
    ticket_id = m.group(0) if m else None

    # Extract tools_called, escalated, and file_search hits from run steps
    tools_called: list[str] = []
    files_searched: list[str] = []  # filenames retrieved via FileSearch
    escalated = False
    for step in client.run_steps.list(thread_id=thread.id, run_id=run.id):
        if step.type == "tool_calls" and step.step_details:
            for tc in step.step_details.tool_calls:
                if hasattr(tc, "function"):
                    tools_called.append(tc.function.name)
                    if tc.function.name == "escalate_to_human":
                        escalated = True
                elif tc.type == "code_interpreter":
                    tools_called.append("code_interpreter")
                elif tc.type == "file_search" and hasattr(tc, "file_search"):
                    results = getattr(tc.file_search, "results", None) or []
                    for r in results:
                        fname = getattr(r, "file_name", None)
                        if fname and fname not in files_searched:
                            files_searched.append(fname)

    internal_summary = reply.split(".")[0].strip() if reply else ""

    return SpecialistResult(
        agent_key=agent_key,
        skill_name=agent.name,
        reply=reply,
        ticket_id=ticket_id,
        escalated=escalated,
        tools_called=tools_called,
        files_searched=files_searched,
        internal_summary=internal_summary,
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
