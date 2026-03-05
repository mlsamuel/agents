"""
skills.py — Skill definitions backed by Postgres with versioning.

On first call to get_pool():
  - Connects to DATABASE_URL
  - Creates the skills table if not present
  - Seeds from skills/**/*.md if the table is empty
  - Populates the in-memory cache (queue → list of skill dicts)

Public sync API (safe to call anywhere, including inside asyncio.run()):
  load_sync(queue)   -> list[dict]   — active skills for a queue (from cache)
  load_all_sync()    -> dict         — all active skills keyed by name (from cache)

Public async API (for improver --apply):
  get_pool()                                    — init + seed + cache
  upsert_version(name, queue, types, tools, content) -> int  — new version, deactivates old
  insert_new(name, queue, types, tools, content)             — v1 for a brand-new skill
"""

import json
import os
from pathlib import Path

import asyncpg
import yaml

from logger import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_cache: dict[str, list[dict]] = {}   # queue_key → [{name, types, tools, system_prompt}]
_SKILLS_DIR = Path(__file__).parent / "data" / "skills"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    queue      TEXT NOT NULL,
    version    INT  NOT NULL DEFAULT 1,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    types      TEXT[] NOT NULL,
    tools      TEXT[] NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);
"""


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter and body. Returns ({}, text) if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].strip()


async def _seed(conn: asyncpg.Connection) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM skills")
    if count > 0:
        log.debug("skills: %d rows already in DB, skipping seed", count)
        return

    if not _SKILLS_DIR.exists():
        log.warning("skills: skills/ directory not found and table is empty — no seed possible")
        return

    rows = []
    for md in sorted(_SKILLS_DIR.glob("**/*.md")):
        queue = md.parent.name   # directory name = queue key (e.g. "billing")
        meta, body = _parse_frontmatter(md.read_text())
        rows.append((
            meta.get("name", md.stem),
            queue,
            meta.get("types", []),
            meta.get("tools", []),
            body,
        ))

    await conn.executemany(
        """INSERT INTO skills (name, queue, version, is_active, types, tools, content)
           VALUES ($1, $2, 1, TRUE, $3, $4, $5)
           ON CONFLICT (name, version) DO NOTHING""",
        rows,
    )
    log.info("skills: seeded %d entries from %s", len(rows), _SKILLS_DIR)


async def _populate_cache(conn: asyncpg.Connection) -> None:
    global _cache
    rows = await conn.fetch(
        "SELECT name, queue, types, tools, content FROM skills WHERE is_active = TRUE"
    )
    cache: dict[str, list[dict]] = {}
    for r in rows:
        q = r["queue"]
        cache.setdefault(q, []).append({
            "name": r["name"],
            "queue": q,
            "types": list(r["types"]),
            "tools": list(r["tools"]),
            "system_prompt": r["content"],
        })
    _cache = cache
    log.debug("skills: cache loaded — %d queues, %d skills total",
              len(_cache), sum(len(v) for v in _cache.values()))


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
        await _populate_cache(conn)
    log.info("skills: pool ready")
    return _pool


# ── Sync read API (safe inside asyncio.run() — reads from cache) ──────────────

def load_sync(queue: str) -> list[dict]:
    """Return active skills for the given queue key. Falls back to 'general' if empty."""
    result = _cache.get(queue)
    if not result:
        result = _cache.get("general", [])
    return result


def load_all_sync() -> dict[str, dict]:
    """Return all active skills as {name: {queue, types, tools, content}} for improver."""
    out: dict[str, dict] = {}
    for skills_list in _cache.values():
        for s in skills_list:
            out[s["name"]] = {
                "queue":   s["queue"],
                "types":   s["types"],
                "tools":   s["tools"],
                "content": s["system_prompt"],
            }
    return out


# ── Async write API (for improver --apply) ───────────────────────────────

async def upsert_version(
    name: str, queue: str, types: list, tools: list, content: str
) -> int:
    """Deactivate all existing versions and insert a new active one. Returns new version."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE skills SET is_active = FALSE WHERE name = $1", name
            )
            new_ver: int = await conn.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM skills WHERE name = $1", name
            )
            await conn.execute(
                """INSERT INTO skills (name, queue, version, is_active, types, tools, content)
                   VALUES ($1, $2, $3, TRUE, $4, $5, $6)""",
                name, queue, new_ver, types, tools, content,
            )
        await _populate_cache(conn)
    log.info("skills: upserted '%s' → v%d", name, new_ver)
    return new_ver


async def insert_new(
    name: str, queue: str, types: list, tools: list, content: str
) -> None:
    """Insert a brand-new skill at version 1."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO skills (name, queue, version, is_active, types, tools, content)
               VALUES ($1, $2, 1, TRUE, $3, $4, $5)
               ON CONFLICT (name, version) DO NOTHING""",
            name, queue, types, tools, content,
        )
        await _populate_cache(conn)
    log.info("skills: inserted new skill '%s' v1", name)
