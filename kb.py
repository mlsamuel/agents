"""
kb.py — Knowledge base backed by pgvector.

On first call to get_pool():
  - Connects to DATABASE_URL
  - Creates the knowledge_base table + HNSW index if not present
  - Seeds from data/knowledge_base.json if the table is empty

Public API:
  search(query, category, top_k) -> list[dict]   — ANN search, same return shape as old numpy impl
  insert(entry)                                   — embed + upsert one KB entry
"""

import json
import os
from pathlib import Path

import asyncpg
from fastembed import TextEmbedding

from logger import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_model: TextEmbedding | None = None
_KB_JSON = Path(__file__).parent / "data" / "knowledge_base.json"

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_base (
    id        TEXT PRIMARY KEY,
    category  TEXT NOT NULL,
    topic     TEXT NOT NULL,
    question  TEXT NOT NULL,
    answer    TEXT NOT NULL,
    embedding vector(384)
);

CREATE INDEX IF NOT EXISTS knowledge_base_embedding_idx
    ON knowledge_base USING hnsw (embedding vector_cosine_ops);
"""


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return _model


def _to_vec_str(arr) -> str:
    return "[" + ",".join(str(float(x)) for x in arr) + "]"


async def _seed(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM knowledge_base")
    if count > 0:
        log.debug("kb: %d entries already in DB, skipping seed", count)
        return

    entries = json.loads(_KB_JSON.read_text())
    model = _get_model()
    embeddings = list(model.embed([e["question"] for e in entries]))

    rows = [
        (
            e["id"], e["category"], e["topic"], e["question"], e["answer"],
            _to_vec_str(embeddings[i]),
        )
        for i, e in enumerate(entries)
    ]
    await conn.executemany(
        """INSERT INTO knowledge_base (id, category, topic, question, answer, embedding)
           VALUES ($1, $2, $3, $4, $5, $6::vector)
           ON CONFLICT (id) DO NOTHING""",
        rows,
    )
    log.info("kb: seeded %d entries from %s", len(rows), _KB_JSON)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    _pool = await asyncpg.create_pool(url)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
        await _seed(conn)
    log.info("kb: pool ready")
    return _pool


async def search(query: str, category: str = "", top_k: int = 3) -> list[dict]:
    """ANN search over the knowledge base. Returns up to top_k entries with score >= 0.25."""
    pool = await get_pool()
    q_str = _to_vec_str(next(_get_model().embed([query])))

    # Fetch extra rows when category filtering so we have enough after the in-Python filter.
    fetch_n = top_k * 4 if category else top_k

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, category, topic, question, answer,
                      1 - (embedding <=> $1::vector) AS score
               FROM knowledge_base
               ORDER BY embedding <=> $1::vector
               LIMIT $2""",
            q_str, fetch_n,
        )

    results = []
    for r in rows:
        score = float(r["score"])
        if score < 0.25:
            continue
        if category and r["category"] != category:
            continue
        results.append({
            "id": r["id"],
            "category": r["category"],
            "topic": r["topic"],
            "question": r["question"],
            "answer": r["answer"],
            "score": round(score, 3),
        })
        if len(results) == top_k:
            break

    return results


async def insert(entry: dict) -> None:
    """Embed entry['question'] and upsert the row into knowledge_base."""
    pool = await get_pool()
    vec_str = _to_vec_str(next(_get_model().embed([entry["question"]])))

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO knowledge_base (id, category, topic, question, answer, embedding)
               VALUES ($1, $2, $3, $4, $5, $6::vector)
               ON CONFLICT (id) DO UPDATE SET
                   category  = EXCLUDED.category,
                   topic     = EXCLUDED.topic,
                   question  = EXCLUDED.question,
                   answer    = EXCLUDED.answer,
                   embedding = EXCLUDED.embedding""",
            entry["id"], entry["category"], entry["topic"],
            entry["question"], entry["answer"], vec_str,
        )
    log.info("kb: upserted entry %s", entry["id"])
