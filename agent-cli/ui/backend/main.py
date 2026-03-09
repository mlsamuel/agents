"""
ui/backend/main.py — FastAPI backend for the agent-cli showcase UI.

Reads from pipeline_runs and pipeline_results tables.

Usage:
    cd agent-cli/ui/backend
    uvicorn main:app --port 8000 --reload
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).parent.parent.parent / ".env")

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    _pool = await asyncpg.create_pool(url)
    yield
    await _pool.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/runs")
async def list_runs():
    rows = await _pool.fetch(
        """SELECT id, run_at, limit_, offset_, language, total,
                  avg_action, avg_completeness, avg_tone, avg_overall
           FROM pipeline_runs
           ORDER BY run_at DESC
           LIMIT 50"""
    )
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}/results")
async def get_results(run_id: int):
    run = await _pool.fetchrow("SELECT id FROM pipeline_runs WHERE id = $1", run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    rows = await _pool.fetch(
        """SELECT id, email_index, subject, body, queue, email_type, priority,
                  skills, tools, ground_truth, generated, internal_summary,
                  score_action, score_completeness, score_tone, score_avg, score_comment
           FROM pipeline_results
           WHERE run_id = $1
           ORDER BY email_index""",
        run_id,
    )
    return [dict(r) for r in rows]
