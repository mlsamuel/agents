"""
routing.py — Conditional edge functions for the main pipeline StateGraph.

All functions are pure predicates: they read PipelineState and return a
string (node name or END) that LangGraph uses to determine the next node.
"""

from langgraph.graph import END

from state import PipelineState

MIN_IMPROVE_SCORE = 4.5   # trigger improvement when avg eval < this


def route_screen(state: PipelineState) -> str:
    """After screen_node: blocked emails go to END; safe emails continue."""
    if not state.get("screen_passed"):
        return END
    return "sanitize"


def route_escalation(state: PipelineState) -> str:
    """After fan-in from specialist sub-graphs.

    If any agent result has escalated=True and no human decision has been
    provided yet, route to wait_for_human (interrupt). Otherwise, proceed
    directly to merge.
    """
    any_escalated = any(r.get("escalated") for r in state.get("agent_results") or [])
    if any_escalated and state.get("human_decision") is None:
        return "wait_for_human"
    return "merge"


def route_eval(state: PipelineState) -> str:
    """After merge_node: skip eval if no ground truth or no reply."""
    if not (state.get("email") or {}).get("answer"):
        return END
    if not state.get("final_reply"):
        return END
    return "eval"


def route_improve(state: PipelineState) -> str:
    """After eval_node: trigger improvement when avg score is below threshold."""
    avg = state.get("eval_avg")
    if avg is not None and avg < MIN_IMPROVE_SCORE:
        return "improve"
    return END
