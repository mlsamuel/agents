"""
routing.py — Conditional edge functions for the main pipeline StateGraph.

All functions are pure predicates: they read PipelineState and return a
string (node name or END) that LangGraph uses to determine the next node.
"""

from langgraph.graph import END

from state import PipelineState

MIN_IMPROVE_SCORE = 4.5   # trigger improvement when avg eval < this
MAX_RETRIES       = 1     # max improve+retry cycles per email


def route_screen(state: PipelineState) -> str:
    """After screen_node: blocked emails go to END; safe emails continue."""
    if not state.get("screen_passed"):
        return END
    return "sanitize"


def _any_escalated_latest(state: PipelineState) -> bool:
    """True if the latest attempt's results contain an escalation with no human decision yet."""
    n = len(state.get("agent_keys") or [])
    latest = (state.get("agent_results") or [])[-n:] if n else (state.get("agent_results") or [])
    return any(r.get("escalated") for r in latest) and state.get("human_decision") is None


def route_after_merge(state: PipelineState) -> str:
    """After merge_node.

    If ground truth is available, eval the reply first.
    Otherwise check escalation directly and either pause or end.
    """
    if (state.get("email") or {}).get("answer") and state.get("final_reply"):
        return "eval"
    if _any_escalated_latest(state):
        return "wait_for_human"
    return END


def route_after_eval(state: PipelineState) -> str:
    """After eval_node.

    If the score is below threshold and retries remain, improve then cycle back
    to fan_out to run the agents again with updated skills.
    Once the score is acceptable or retries are exhausted, check for escalation.
    """
    avg         = state.get("eval_avg")
    retry_count = state.get("retry_count", 0)

    if avg is not None and avg < MIN_IMPROVE_SCORE and retry_count < MAX_RETRIES:
        return "improve"

    if _any_escalated_latest(state):
        return "wait_for_human"
    return END
