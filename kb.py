"""
kb.py — Knowledge base backed by pgvector.

On first call to get_pool():
  - Connects to DATABASE_URL
  - Creates the knowledge_base table + HNSW index if not present
  - Seeds from data/knowledge_base.json if the table is empty

Public API:
  search(query, category, top_k) -> list[dict]
  insert(entry)          -> int   — insert new entry (v1), returns assigned id
  upsert_version(entry)  -> int   — deactivate existing active, insert new version
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
    id         SERIAL PRIMARY KEY,
    category   TEXT NOT NULL,
    topic      TEXT NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    keywords   TEXT[] NOT NULL DEFAULT '{}',
    version    INT NOT NULL DEFAULT 1,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding  vector(384),
    UNIQUE (topic, version)
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
            e.get("keywords", []), _to_vec_str(embeddings[i]),
        )
        for i, e in enumerate(entries)
    ]
    await conn.executemany(
        """INSERT INTO knowledge_base (id, category, topic, question, answer, keywords, embedding)
           VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
           ON CONFLICT (id) DO NOTHING""",
        rows,
    )
    # Advance SERIAL sequence past seeded IDs so new inserts don't collide
    await conn.execute(
        "SELECT setval(pg_get_serial_sequence('knowledge_base', 'id'), MAX(id)) FROM knowledge_base"
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
    """ANN search over active KB entries. Returns up to top_k entries with score >= 0.25."""
    pool = await get_pool()
    q_str = _to_vec_str(next(_get_model().embed([query])))

    fetch_n = top_k * 4 if category else top_k

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, category, topic, question, answer, keywords,
                      1 - (embedding <=> $1::vector) AS score
               FROM knowledge_base
               WHERE is_active = TRUE
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
            "id":       r["id"],
            "category": r["category"],
            "topic":    r["topic"],
            "question": r["question"],
            "answer":   r["answer"],
            "keywords": list(r["keywords"]),
            "score":    round(score, 3),
        })
        if len(results) == top_k:
            break

    return results


async def insert(entry: dict) -> int:
    """Embed and insert a new KB entry at version 1. Returns the assigned integer id."""
    pool = await get_pool()
    vec_str = _to_vec_str(next(_get_model().embed([entry["question"]])))
    keywords = entry.get("keywords", [])

    async with pool.acquire() as conn:
        new_id = await conn.fetchval(
            """INSERT INTO knowledge_base
                   (category, topic, question, answer, keywords, embedding)
               VALUES ($1, $2, $3, $4, $5, $6::vector)
               RETURNING id""",
            entry["category"], entry["topic"], entry["question"],
            entry["answer"], keywords, vec_str,
        )
    log.info("kb: inserted entry %d (%s)", new_id, entry.get("topic", ""))
    return new_id


async def upsert_version(entry: dict) -> int:
    """Deactivate existing active entry for same topic and insert a new version. Returns new id."""
    pool = await get_pool()
    vec_str = _to_vec_str(next(_get_model().embed([entry["question"]])))
    keywords = entry.get("keywords", [])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE knowledge_base SET is_active = FALSE
                   WHERE topic = $1 AND category = $2 AND is_active = TRUE""",
                entry["topic"], entry["category"],
            )
            new_ver = await conn.fetchval(
                """SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_base
                   WHERE topic = $1 AND category = $2""",
                entry["topic"], entry["category"],
            )
            new_id = await conn.fetchval(
                """INSERT INTO knowledge_base
                       (category, topic, question, answer, keywords, version, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                   RETURNING id""",
                entry["category"], entry["topic"], entry["question"],
                entry["answer"], keywords, new_ver, vec_str,
            )
    log.info("kb: upserted '%s' → v%d (id=%d)", entry.get("topic", ""), new_ver, new_id)
    return new_id
