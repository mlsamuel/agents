from langgraph.graph.state import CompiledStateGraph
from .base_agent import build_agent_graph

_graph: CompiledStateGraph | None = None


def get_graph() -> CompiledStateGraph:
    global _graph
    if _graph is None:
        _graph = build_agent_graph("returns")
    return _graph
