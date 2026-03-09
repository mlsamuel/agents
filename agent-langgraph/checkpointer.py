"""
checkpointer.py — AsyncPostgresSaver setup for interrupt() persistence.

Uses the same Postgres database as the knowledge base (agents_langgraph),
with separate tables for LangGraph checkpointing:
  - checkpoints
  - checkpoint_blobs
  - checkpoint_writes
  - checkpoint_migrations

Tables are created automatically on first call to setup().

The checkpointer enables:
  1. interrupt() in wait_for_human_node — pipeline state is persisted to Postgres
     before the node runs, allowing the process to exit and resume later.
  2. Resume via: graph.ainvoke(Command(resume=decision), config=thread_config)

Note: AsyncPostgresSaver uses psycopg3 (the psycopg package), not asyncpg.
Both can coexist in the same process — asyncpg is used by store.py for the KB,
psycopg3 is used here for checkpointing.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


async def get_checkpointer():
    """
    Create and return an AsyncPostgresSaver backed by the agents_langgraph database.

    Calls setup() on first use to create the checkpoint tables if they don't exist.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set. "
            "Add it to .env: postgresql://agents:agents@localhost:5432/agents_langgraph"
        )

    checkpointer = AsyncPostgresSaver.from_conn_string(db_url)
    await checkpointer.setup()
    return checkpointer
