"""
ui/backend/main.py — FastAPI backend for the escalation review UI.

Serves pending and resolved escalations from the escalation_queue table.
When a human makes a decision, writes it to the DB with status='decided'.

The pipeline service (pipeline.py --serve) polls for 'decided' rows and is
the sole process that resumes LangGraph pipelines — this backend never imports
the graph or checkpointer.

Usage (run from agent-langgraph/ dir so store.py is importable):
    uvicorn ui.backend.main:app --port 8001 --reload
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Allow imports from the agent-langgraph root (store.py, logger.py, …)
_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.get_pool()
    yield
    if store._pool:
        await store._pool.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _serialize(row: dict) -> dict:
    """Convert asyncpg record fields to JSON-serialisable types."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/escalations")
async def list_escalations():
    """Return all escalations (pending + decided + resolved), newest first."""
    rows = await store.get_all_escalations()
    return [_serialize(r) for r in rows]


class DecisionRequest(BaseModel):
    decision: str  # "approve" | "override: <text>"


@app.post("/api/escalations/{thread_id}/decide")
async def decide(thread_id: str, body: DecisionRequest):
    """
    Record a human decision.

    Sets status='decided' in escalation_queue. The pipeline service
    (pipeline.py --serve) polls for 'decided' rows and resumes the graph.
    """
    decision = body.decision.strip()
    if not decision:
        raise HTTPException(status_code=400, detail="decision must not be empty")

    rows = await store.get_all_escalations()
    existing = next((r for r in rows if r["thread_id"] == thread_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    if existing["status"] != "pending":
        return {"status": "already_decided", "current_status": existing["status"]}

    await store.submit_decision(thread_id, decision)
    return {"status": "queued"}
