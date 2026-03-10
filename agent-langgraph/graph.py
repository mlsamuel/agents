"""
graph.py — Builds and compiles the main pipeline StateGraph.

Pipeline flow:
  START
    → screen               (Haiku: injection detection)
    → [route_screen]       → END (quarantined) | sanitize
    → sanitize             (regex: strip injection patterns)
    → classify             (Haiku: queue / priority / type)
    → decompose            (Haiku: which specialist agents to invoke)
    → fan_out              (Send API → parallel specialist sub-graphs)
      ↓↓↓ parallel fan-out via Send — all branches join at merge
    → billing_agent | technical_agent | returns_agent | general_agent
    → merge                (Sonnet: synthesise final_reply)
    → [route_after_merge]  → eval | wait_for_human | END
    → eval                 (Haiku: LLM-as-judge scoring)
    → [route_after_eval]   → improve | wait_for_human | END
    → improve              (Sonnet: generate + apply improvement proposals)
    → retry                ← no-op pass-through enabling the improve → fan_out cycle
    → wait_for_human       (interrupt() — pauses for human review)
    → END

LangGraph patterns demonstrated:
  • StateGraph + TypedDict state with reducers (Annotated[list, operator.add])
  • Send API for parallel fan-out (replaces asyncio.gather in orchestrator_agent.py)
  • Compiled sub-graphs invoked as async node wrappers
  • Cycle in main graph — improve → fan_out retry loop with retry_count guard
  • interrupt() for human-in-the-loop escalation review (after eval+improve)
  • Conditional edges for pipeline branching at every decision point
  • AsyncPostgresSaver checkpointer for state persistence across interrupt()
"""

from langgraph.graph import END, START, StateGraph

from agents.billing   import get_graph as billing_graph
from agents.general   import get_graph as general_graph
from agents.returns   import get_graph as returns_graph
from agents.technical import get_graph as technical_graph

from nodes import (
    classify_node,
    decompose_node,
    eval_node,
    fan_out_node,
    improve_node,
    merge_node,
    retry_node,
    sanitize_node,
    screen_node,
    wait_for_human_node,
    wrap_agent_result,
)
from routing import route_after_eval, route_after_merge, route_screen
from state import AgentState, PipelineState


# ── Specialist node wrappers ──────────────────────────────────────────────────
# Each wrapper:
#   1. Receives an AgentState dict (sent via Send from fan_out_node)
#   2. Runs the compiled specialist sub-graph asynchronously
#   3. Calls wrap_agent_result to convert AgentState → PipelineState update
#      (appends one AgentResult to agent_results via operator.add reducer)

async def billing_agent_node(agent_state: AgentState) -> dict:
    final = await billing_graph().ainvoke(agent_state)
    return wrap_agent_result(final)


async def technical_agent_node(agent_state: AgentState) -> dict:
    final = await technical_graph().ainvoke(agent_state)
    return wrap_agent_result(final)


async def returns_agent_node(agent_state: AgentState) -> dict:
    final = await returns_graph().ainvoke(agent_state)
    return wrap_agent_result(final)


async def general_agent_node(agent_state: AgentState) -> dict:
    final = await general_graph().ainvoke(agent_state)
    return wrap_agent_result(final)


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_main_graph(checkpointer=None):
    """
    Build and compile the main pipeline graph.

    Args:
        checkpointer: LangGraph checkpointer for interrupt() persistence.
                      Pass an AsyncPostgresSaver for production use.
                      Pass None (or MemorySaver) for testing without persistence.

    Returns:
        CompiledStateGraph ready for ainvoke() / stream().
    """
    builder = StateGraph(PipelineState)

    # ── Pipeline nodes ───────────────────────────────────────────────────────
    builder.add_node("screen",         screen_node)
    builder.add_node("sanitize",       sanitize_node)
    builder.add_node("classify",       classify_node)
    builder.add_node("decompose",      decompose_node)
    # retry_node is a no-op pass-through that makes the improve → fan_out cycle
    # possible. LangGraph only accepts list[Send] from conditional edge routing
    # functions, not from registered nodes. fan_out_node is therefore kept as a
    # routing function; retry_node is the addressable target for improve.
    builder.add_node("retry",          retry_node)
    builder.add_node("wait_for_human", wait_for_human_node)
    builder.add_node("merge",          merge_node)
    builder.add_node("eval",           eval_node)
    builder.add_node("improve",        improve_node)

    # ── Specialist sub-graph nodes ───────────────────────────────────────────
    # Registered by agent_key → node name.
    # Receive AgentState via Send, return PipelineState update via wrap_agent_result.
    builder.add_node("billing_agent",   billing_agent_node)
    builder.add_node("technical_agent", technical_agent_node)
    builder.add_node("returns_agent",   returns_agent_node)
    builder.add_node("general_agent",   general_agent_node)

    # ── Edges ────────────────────────────────────────────────────────────────
    builder.add_edge(START, "screen")

    builder.add_conditional_edges(
        "screen", route_screen,
        {END: END, "sanitize": "sanitize"},
    )

    builder.add_edge("sanitize", "classify")
    builder.add_edge("classify", "decompose")

    # fan_out_node is used as a conditional edge routing function (returns list[Send]).
    # Both decompose and retry feed into it via the same pattern.
    builder.add_conditional_edges("decompose", fan_out_node)

    # All specialist branches converge at merge via direct edges.
    # operator.add reducer on agent_results ensures parallel branches don't
    # overwrite each other — LangGraph waits for all branches before calling merge.
    for specialist in ["billing_agent", "technical_agent", "returns_agent", "general_agent"]:
        builder.add_edge(specialist, "merge")

    # merge → eval (if ground truth) | wait_for_human (if escalated) | END
    builder.add_conditional_edges(
        "merge", route_after_merge,
        {END: END, "eval": "eval", "wait_for_human": "wait_for_human"},
    )

    # eval → improve (score low, retries remain) | wait_for_human (escalated) | END
    builder.add_conditional_edges(
        "eval", route_after_eval,
        {END: END, "improve": "improve", "wait_for_human": "wait_for_human"},
    )

    # improve → retry → [fan_out_node routing fn] → specialists: retry cycle.
    # retry_node is the addressable target; fan_out_node is reused as the routing
    # function that returns list[Send]. retry_count bounds this to MAX_RETRIES.
    builder.add_edge("improve", "retry")
    builder.add_conditional_edges("retry", fan_out_node)

    # After human reviews the escalation, the pipeline ends.
    # eval+improve have already run before reaching this node.
    builder.add_edge("wait_for_human", END)

    # ── Compile ──────────────────────────────────────────────────────────────
    compile_kwargs: dict = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer

    return builder.compile(**compile_kwargs)
