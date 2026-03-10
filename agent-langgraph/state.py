"""
state.py — TypedDict state definitions for the LangGraph pipeline.

Two graphs, two states:

  PipelineState  — top-level pipeline StateGraph
                   tracks the email from screen → classify → fan-out → merge → eval → improve

  AgentState     — specialist agent sub-graph (one per queue)
                   tracks the multi-turn tool-use loop + reflection cycle
"""

import operator
from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


# ── Nested dicts (for readability — not enforced at runtime) ──────────────────

class EmailInput(TypedDict):
    subject: str
    body: str
    answer: str        # ground truth reply (used by eval)
    queue: str
    priority: str
    type: str
    language: str


class Classification(TypedDict):
    queue: str         # "Technical Support" | "Billing and Payments" | "Returns and Exchanges" | "General Inquiry"
    priority: str      # "critical" | "high" | "medium" | "low" | "very_low"
    type: str          # "Incident" | "Problem" | "Request" | "Change" | "Question" | "Complaint"
    reason: str


class AgentResult(TypedDict):
    agent_key: str     # "billing" | "technical_support" | "returns" | "general"
    skill_used: str
    ticket_id: str
    reply_drafted: str
    internal_summary: str
    escalated: bool
    action: str        # "resolved" | "escalated" | "replied" | "pending"
    tool_calls: list[dict]
    revision_count: int


# ── Main pipeline state ───────────────────────────────────────────────────────

class PipelineState(TypedDict):
    # Input
    email: EmailInput

    # Security screening
    screen_passed: bool
    screen_reason: str

    # Classification
    classification: Classification

    # Decomposition — which specialist agents to invoke
    agent_keys: list[str]
    parallel: bool

    # Fan-out results.
    # Annotated with operator.add so parallel Send() branches each append one entry
    # without clobbering each other.
    agent_results: Annotated[list[AgentResult], operator.add]

    # Merged output
    final_reply: str
    action: str        # "resolved" | "escalated" | "replied" | "pending"

    # Human-in-the-loop (interrupt / resume)
    escalation_pending: bool
    human_decision: str | None  # set when pipeline is resumed: "approve" | "override: <text>"

    # Evaluation (populated after reply is produced, if --eval)
    eval_score: dict | None    # {action: int, completeness: int, tone: int, comment: str}
    eval_avg: float | None

    # Retry loop — incremented by improve_node each cycle (max 1 retry)
    retry_count: int


# ── Specialist agent sub-graph state ─────────────────────────────────────────

class AgentState(TypedDict):
    # Inputs (set once before sub-graph starts)
    email: EmailInput
    classification: Classification
    agent_key: str     # which specialist this is
    skill: dict        # {name, agent, types, tools, system_prompt}

    # LangGraph manages the message list via add_messages reducer.
    # Each turn: agent appends AIMessage, ToolNode appends ToolMessages.
    messages: Annotated[list[BaseMessage], add_messages]

    # Extracted state, updated as the tool-use loop progresses
    ticket_id: str
    reply_drafted: str
    escalated: bool
    tool_calls: list[dict]  # audit log: [{tool, input, result}, ...]

    # Reflection loop
    revision_count: int         # incremented by critic each cycle (max 2)
    critic_feedback: str | None # critic's suggested improvement; None means accept

    # run_code retry loop
    run_code_retries: int              # counts run_code failures in this sub-graph turn
    pending_code_retry_prompt: str | None  # injected into next agent turn; consumed and cleared

    # Final packed result — set by critic node when done
    result: AgentResult | None
