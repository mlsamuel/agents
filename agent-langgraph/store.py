"""
store.py — Knowledge base, agent guidelines, regression training set, and pipeline runs backed by pgvector.

On first call to get_pool():
  - Connects to DATABASE_URL
  - Creates tables + HNSW indexes if not present
  - Seeds knowledge_base from data/knowledge_base.json if empty
  - Seeds agent_guidelines from data/agent_guidelines.json if empty
  - Seeds training_set from data/training_set.json if empty

Public API (knowledge base):
  search(query, category, top_k)    -> list[dict]
  insert(entry)                     -> int
  upsert_version(entry)             -> int

Public API (agent guidelines):
  search_guideline(query, category, top_k) -> list[dict]
  insert_guideline(entry)                  -> int
  upsert_guideline_version(entry)          -> int

Public API (regression training set):
  get_training(skill_name)                          -> list[dict]
  add_training_email(skill_name, subject, body, answer) -> bool

Public API (pipeline runs):
  create_run(limit_, offset_, language)             -> int
  store_result(run_id, section)                     -> None
  update_run_stats(run_id, sections)                -> None

Public API (escalation queue):
  add_escalation(thread_id, subject, ...)           -> None  (called by pipeline on interrupt)
  submit_decision(thread_id, human_decision)        -> None  (called by UI backend on human action)
  get_decided_escalations()                         -> list[dict]  (polled by pipeline --serve)
  resolve_escalation(thread_id, status, decision)   -> None  (called by pipeline after resume)
  get_pending_escalations()                         -> list[dict]
  get_all_escalations()                             -> list[dict]
"""

import json
import os
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import asyncpg
from fastembed import TextEmbedding

from logger import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_model: TextEmbedding | None = None
_KB_JSON          = Path(__file__).parent / "data" / "knowledge_base.json"
_GUIDELINES_JSON  = Path(__file__).parent / "data" / "agent_guidelines.json"
_TRAINING_JSON    = Path(__file__).parent / "data" / "training_set.json"

REGRESSION_THRESHOLD = 3.5
MAX_PER_SKILL        = 3

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

CREATE TABLE IF NOT EXISTS agent_guidelines (
    id          SERIAL PRIMARY KEY,
    category    TEXT NOT NULL,
    topic       TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    instruction TEXT NOT NULL,
    keywords    TEXT[] NOT NULL DEFAULT '{}',
    version     INT NOT NULL DEFAULT 1,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding   vector(384),
    UNIQUE (topic, version)
);

CREATE INDEX IF NOT EXISTS agent_guidelines_embedding_idx
    ON agent_guidelines USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS training_set (
    id         SERIAL PRIMARY KEY,
    skill_name TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id               SERIAL PRIMARY KEY,
    run_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    limit_           INT,
    offset_          INT,
    language         TEXT,
    total            INT,
    avg_action       FLOAT,
    avg_completeness FLOAT,
    avg_tone         FLOAT,
    avg_overall      FLOAT
);

CREATE TABLE IF NOT EXISTS pipeline_results (
    id               SERIAL PRIMARY KEY,
    run_id           INT  NOT NULL REFERENCES pipeline_runs(id),
    email_index      INT  NOT NULL,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    queue            TEXT NOT NULL,
    email_type       TEXT NOT NULL,
    priority         TEXT NOT NULL,
    skills           TEXT,
    tools            TEXT,
    ground_truth     TEXT NOT NULL,
    generated        TEXT NOT NULL,
    internal_summary TEXT,
    score_action        INT,
    score_completeness  INT,
    score_tone          INT,
    score_avg           FLOAT,
    score_comment       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS escalation_queue (
    thread_id        TEXT PRIMARY KEY,
    subject          TEXT NOT NULL,
    body             TEXT,
    queue            TEXT,
    priority         TEXT,
    email_type       TEXT,
    escalated_agents TEXT[] NOT NULL DEFAULT '{}',
    summaries        TEXT[] NOT NULL DEFAULT '{}',
    draft_replies    TEXT[] NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending',
    human_decision   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at       TIMESTAMPTZ
);
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


async def _seed_guidelines(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM agent_guidelines")
    if count > 0:
        log.debug("kb: %d guidelines already in DB, skipping seed", count)
        return

    if not _GUIDELINES_JSON.exists():
        return

    entries = json.loads(_GUIDELINES_JSON.read_text())
    if not entries:
        return

    model = _get_model()
    embeddings = list(model.embed([e["trigger"] for e in entries]))
    rows = [
        (
            e["category"], e["topic"], e["trigger"], e["instruction"],
            e.get("keywords", []), _to_vec_str(embeddings[i]),
        )
        for i, e in enumerate(entries)
    ]
    await conn.executemany(
        """INSERT INTO agent_guidelines
               (category, topic, trigger, instruction, keywords, embedding)
           VALUES ($1, $2, $3, $4, $5, $6::vector)
           ON CONFLICT (topic, version) DO NOTHING""",
        rows,
    )
    log.info("kb: seeded %d guidelines from %s", len(rows), _GUIDELINES_JSON)


async def _seed_training(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM training_set")
    if count > 0:
        log.debug("kb: %d training emails already in DB, skipping seed", count)
        return

    data = json.loads(_TRAINING_JSON.read_text())
    rows = []
    for skill_name, emails in data.items():
        for e in emails:
            rows.append((skill_name, e["subject"], e["body"], e["answer"]))
    if rows:
        await conn.executemany(
            "INSERT INTO training_set (skill_name, subject, body, answer) VALUES ($1, $2, $3, $4)",
            rows,
        )
    log.info("kb: seeded %d training emails from %s", len(rows), _TRAINING_JSON)


async def _ensure_db(url: str) -> None:
    """Create the database if it does not exist."""
    try:
        conn = await asyncpg.connect(url)
        await conn.close()
    except asyncpg.InvalidCatalogNameError:
        db_name = url.rsplit("/", 1)[-1].split("?")[0]
        admin_url = url.rsplit("/", 1)[0] + "/postgres"
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            log.info("kb: created database %s", db_name)
        finally:
            await conn.close()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    await _ensure_db(url)
    _pool = await asyncpg.create_pool(url)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
        await _seed(conn)
        await _seed_guidelines(conn)
        await _seed_training(conn)
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


# ── Agent guidelines ──────────────────────────────────────────────────────────

async def search_guideline(query: str, category: str = "", top_k: int = 3) -> list[dict]:
    """ANN search over active agent guidelines. Returns up to top_k entries with score >= 0.25."""
    pool = await get_pool()
    q_str = _to_vec_str(next(_get_model().embed([query])))

    fetch_n = top_k * 4 if category else top_k

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, category, topic, trigger, instruction, keywords,
                      1 - (embedding <=> $1::vector) AS score
               FROM agent_guidelines
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
            "id":          r["id"],
            "category":    r["category"],
            "topic":       r["topic"],
            "trigger":     r["trigger"],
            "instruction": r["instruction"],
            "keywords":    list(r["keywords"]),
            "score":       round(score, 3),
        })
        if len(results) == top_k:
            break

    return results


async def insert_guideline(entry: dict) -> int:
    """Embed and insert a new agent guideline at version 1. Returns the assigned integer id."""
    pool = await get_pool()
    vec_str = _to_vec_str(next(_get_model().embed([entry["trigger"]])))
    keywords = entry.get("keywords", [])

    async with pool.acquire() as conn:
        new_id = await conn.fetchval(
            """INSERT INTO agent_guidelines
                   (category, topic, trigger, instruction, keywords, embedding)
               VALUES ($1, $2, $3, $4, $5, $6::vector)
               RETURNING id""",
            entry["category"], entry["topic"], entry["trigger"],
            entry["instruction"], keywords, vec_str,
        )
    log.info("kb: inserted guideline %d (%s)", new_id, entry.get("topic", ""))
    return new_id


async def upsert_guideline_version(entry: dict) -> int:
    """Deactivate existing active guideline for same topic and insert a new version. Returns new id."""
    pool = await get_pool()
    vec_str = _to_vec_str(next(_get_model().embed([entry["trigger"]])))
    keywords = entry.get("keywords", [])

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE agent_guidelines SET is_active = FALSE
                   WHERE topic = $1 AND category = $2 AND is_active = TRUE""",
                entry["topic"], entry["category"],
            )
            new_ver = await conn.fetchval(
                """SELECT COALESCE(MAX(version), 0) + 1 FROM agent_guidelines
                   WHERE topic = $1 AND category = $2""",
                entry["topic"], entry["category"],
            )
            new_id = await conn.fetchval(
                """INSERT INTO agent_guidelines
                       (category, topic, trigger, instruction, keywords, version, embedding)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                   RETURNING id""",
                entry["category"], entry["topic"], entry["trigger"],
                entry["instruction"], keywords, new_ver, vec_str,
            )
    log.info("kb: upserted guideline '%s' → v%d (id=%d)", entry.get("topic", ""), new_ver, new_id)
    return new_id


# ── Regression training set ───────────────────────────────────────────────────

async def get_training(skill_name: str) -> list[dict]:
    """Return all training emails for a skill (up to MAX_PER_SKILL)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT subject, body, answer FROM training_set WHERE skill_name = $1",
            skill_name,
        )
    return [{"subject": r["subject"], "body": r["body"], "answer": r["answer"]} for r in rows]


async def add_training_email(skill_name: str, subject: str, body: str, answer: str) -> bool:
    """Add an email to the training set for skill_name if there is room.

    Returns True if added, False if the skill already has MAX_PER_SKILL entries.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM training_set WHERE skill_name = $1", skill_name
        )
        if count >= MAX_PER_SKILL:
            return False
        await conn.execute(
            "INSERT INTO training_set (skill_name, subject, body, answer) VALUES ($1, $2, $3, $4)",
            skill_name, subject, body, answer,
        )
    log.info("kb: added training email for '%s' (slot %d/%d)", skill_name, count + 1, MAX_PER_SKILL)
    return True


# ── Pipeline runs ─────────────────────────────────────────────────────────────

async def create_run(limit_: int, offset_: int, language: str) -> int:
    """Insert a new pipeline_runs row and return its id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        run_id = await conn.fetchval(
            "INSERT INTO pipeline_runs (limit_, offset_, language) VALUES ($1, $2, $3) RETURNING id",
            limit_, offset_, language,
        )
    return run_id


async def store_result(run_id: int, section: dict) -> None:
    """Insert one pipeline_results row from a section dict."""
    pool = await get_pool()
    score = section.get("score") or {}
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO pipeline_results
               (run_id, email_index, subject, body, queue, email_type, priority,
                skills, tools, ground_truth, generated, internal_summary,
                score_action, score_completeness, score_tone, score_avg, score_comment)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
            run_id,
            section["index"],
            section["subject"],
            section.get("body", ""),
            section.get("queue", ""),
            section.get("type", ""),
            section.get("priority", ""),
            section.get("skills", ""),
            section.get("tools", ""),
            section.get("ground_truth", ""),
            section.get("generated", ""),
            section.get("internal_summary", ""),
            score.get("action"),
            score.get("completeness"),
            score.get("tone"),
            section.get("avg"),
            score.get("comment"),
        )


async def update_run_stats(run_id: int, sections: list[dict]) -> None:
    """Update aggregate scores on a pipeline_runs row after processing completes."""
    if not sections:
        return
    total = len(sections)
    avg_action       = sum(s["score"]["action"]       for s in sections) / total
    avg_completeness = sum(s["score"]["completeness"] for s in sections) / total
    avg_tone         = sum(s["score"]["tone"]         for s in sections) / total
    avg_overall      = sum(s["avg"]                   for s in sections) / total
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE pipeline_runs
               SET total=$2, avg_action=$3, avg_completeness=$4, avg_tone=$5, avg_overall=$6
               WHERE id=$1""",
            run_id, total, avg_action, avg_completeness, avg_tone, avg_overall,
        )


# ── Escalation queue ──────────────────────────────────────────────────────────

async def add_escalation(
    thread_id: str,
    subject: str,
    body: str,
    queue: str,
    priority: str,
    email_type: str,
    escalated_agents: list[str],
    summaries: list[str],
    draft_replies: list[str],
) -> None:
    """Write an escalation row before interrupt() so the UI can list pending reviews.

    Uses INSERT ON CONFLICT DO NOTHING — idempotent if the graph retries the node.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO escalation_queue
               (thread_id, subject, body, queue, priority, email_type,
                escalated_agents, summaries, draft_replies)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (thread_id) DO NOTHING""",
            thread_id, subject, body, queue, priority, email_type,
            escalated_agents, summaries, draft_replies,
        )
    log.info("escalation: queued %s (%s)", thread_id, subject[:60])


async def submit_decision(thread_id: str, human_decision: str) -> None:
    """Record a human decision from the UI — sets status='decided' so pipeline --serve picks it up."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE escalation_queue
               SET status = 'decided', human_decision = $2, decided_at = NOW()
               WHERE thread_id = $1""",
            thread_id, human_decision,
        )
    log.info("escalation: decision submitted for %s: %r", thread_id, human_decision[:60])


async def get_decided_escalations() -> list[dict]:
    """Return escalations with status='decided' that pipeline --serve should resume."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT thread_id, human_decision FROM escalation_queue WHERE status = 'decided'"
        )
    return [dict(r) for r in rows]


async def resolve_escalation(thread_id: str, status: str, human_decision: str) -> None:
    """Mark an escalation as approved or overridden after the pipeline has resumed it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE escalation_queue
               SET status = $2, human_decision = $3, decided_at = NOW()
               WHERE thread_id = $1""",
            thread_id, status, human_decision,
        )
    log.info("escalation: resolved %s → %s", thread_id, status)


async def get_pending_escalations() -> list[dict]:
    """Return all escalations with status='pending', newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT thread_id, subject, body, queue, priority, email_type,
                      escalated_agents, summaries, draft_replies,
                      status, human_decision, created_at, decided_at
               FROM escalation_queue
               WHERE status = 'pending'
               ORDER BY created_at DESC"""
        )
    return [dict(r) for r in rows]


async def get_all_escalations() -> list[dict]:
    """Return all escalations (pending + resolved), newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT thread_id, subject, body, queue, priority, email_type,
                      escalated_agents, summaries, draft_replies,
                      status, human_decision, created_at, decided_at
               FROM escalation_queue
               ORDER BY created_at DESC"""
        )
    return [dict(r) for r in rows]
