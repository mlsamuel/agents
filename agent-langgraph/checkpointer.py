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

# Module-level pool — created once, reused across the pipeline run.
_pool = None


async def get_checkpointer():
    """
    Create and return an AsyncPostgresSaver backed by the agents_langgraph database.

    Uses an AsyncConnectionPool (psycopg3) so the checkpointer stays open for
    the entire pipeline run without needing an async context manager.
    Calls setup() on first use to create the checkpoint tables if they don't exist.
    """
    global _pool

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set. "
            "Add it to .env: postgresql://agents:agents@localhost:5432/agents_langgraph"
        )

    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=db_url,
            max_size=5,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await _pool.open()

    checkpointer = AsyncPostgresSaver(_pool)
    await checkpointer.setup()
    return checkpointer
