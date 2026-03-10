"""
kb_chat.py — Streaming knowledge base chat agent.

Uses LangGraph's create_react_agent (prebuilt) with the existing KB search tool
and AsyncPostgresSaver checkpointer for persistent multi-turn conversations.

Demonstrates:
  - create_react_agent from langgraph.prebuilt (contrast to the custom StateGraph pipeline)
  - astream_events() for real-time token streaming
  - MessagesState (implicit, used by create_react_agent internally)
  - Checkpointer reuse for conversational memory (different from HITL persistence)
"""

import sys
from pathlib import Path

# Allow imports from the agent-langgraph root when running from agents/ subdirectory
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

load_dotenv(_ROOT / ".env")

import store
from checkpointer import get_checkpointer

SYSTEM_PROMPT = """You are a helpful customer support assistant for an e-commerce platform.
You have access to a knowledge base containing answers to common questions about billing,
returns, shipping, and technical issues.

When answering questions:
1. Search the knowledge base first using the search_knowledge_base tool
2. Base your answer on the search results when available
3. Be concise and friendly
4. If the knowledge base doesn't have a relevant answer, say so clearly

You can handle follow-up questions — the conversation history is available to you."""

_graph = None


@tool
async def search_knowledge_base(query: str, category: str = "") -> list:
    """Search the customer support knowledge base.

    Args:
        query:    The question or topic to look up.
        category: Optional filter — billing, returns, technical, general.
                  Leave blank to search all categories.
    """
    return await store.search(query, category, top_k=3)


async def get_chat_graph():
    """Return the compiled chat agent, creating it on first call.

    Uses create_react_agent (langgraph.prebuilt) — a single-file alternative
    to the custom StateGraph used by the main pipeline. Internally uses
    MessagesState and a simple agent → tools → agent loop.

    The same AsyncPostgresSaver checkpointer is reused here for conversation
    memory: each thread_id maps to a separate conversation history.
    """
    global _graph
    if _graph is not None:
        return _graph

    checkpointer = await get_checkpointer()
    model = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=1024)
    _graph = create_react_agent(
        model,
        tools=[search_knowledge_base],
        prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )
    return _graph
