# Customer Support Agent System — LangGraph Edition

A multi-agent customer support pipeline built with LangChain and LangGraph. Emails are classified, routed to specialist workflow agents, and handled using skill files that drive tool selection and reply logic. An integrated eval+improve loop scores replies and automatically updates skills, the knowledge base, and agent guidelines.

This project implements the same pipeline as `agent-mcp` and `agent-cli` but orchestrates it as an explicit **StateGraph**, demonstrating LangGraph-specific patterns: the Send API for parallel fan-out, compiled sub-graphs as nodes, `ToolNode` for in-process tool execution, a reflection loop inside each specialist agent, a retry cycle in the main graph, and `interrupt()` for human-in-the-loop escalation review.

## Architecture

```
email_stream
    │
    ▼
screen (StateGraph node)      ← Haiku: prompt injection detection
    │
sanitize                      ← pattern-based strip of injection attempts
    │
    ▼
classify                      ← Haiku: queue / type / priority
    │
    ▼
decompose                     ← Haiku: which specialist agent(s) to invoke
    │
    ▼  Send API — parallel fan-out (replaces asyncio.gather)
    ├── billing_agent ─────────────────────┐
    ├── technical_agent ───────────────────┤  each is a compiled StateGraph(AgentState)
    ├── returns_agent ─────────────────────┤  with its own agent → tools → critic loop
    └── general_agent ─────────────────────┘
    │                         ↑
    │          ToolNode executes @tool functions in-process
    │          (no subprocess, no HTTP server)
    │
    ▼  fan-in via operator.add reducer on agent_results
merge                         ← Sonnet: synthesise final_reply from all agent_results
    │
    ▼  (when --eval and ground truth available)
eval                          ← Haiku: scores action / completeness / tone
    │
    ├── (--improve and avg < min-score and retry_count < 1)
    │       ▼
    │   improve               ← Sonnet: proposes skill_edit / kb_entry /
    │       │                    agent_guideline / new_skill
    │       └──────────────────► fan_out  ← retry cycle: re-runs agents with updated skills
    │
    ▼  (score ok, max retries, or --no-improve)
[route_after_eval]
    │
    ├── wait_for_human        ← interrupt() inside the node — pauses after writing to
    │                            escalation_queue so the UI can list pending reviews
    │                            UI:     http://localhost:5173
    │                            Manual: python pipeline.py --resume <id> --decision "approve"
    ▼  (not escalated)
END
    │
    ▼
Postgres (skills + knowledge_base + agent_guidelines + training_set +
          pipeline_runs + pipeline_results + escalation_queue +
          LangGraph checkpoint tables)
```

### Specialist agent sub-graph (one per queue)

Each specialist runs its own `StateGraph(AgentState)` with a reflection loop:

```
START → agent → [tool calls?] → tools → agent (loop)
              ↘ [done]       → critic → [score ok or max revisions] → END
                                      ↘ [score low, revisions < 2]  → agent (revise)
```

- **agent** — `ChatAnthropic.bind_tools(skill_filtered_tools)` produces tool calls or a final reply
- **tools** — `ToolNode` executes `@tool` functions in-process (asyncpg KB search, simulated CRM/orders)
- **critic** — Haiku scores the draft reply on completeness + tone; loops back with feedback if needed

### LangGraph patterns demonstrated

| Pattern | Where |
|---|---|
| `StateGraph` + `TypedDict` state | `state.py`, `graph.py`, `agents/base_agent.py` |
| `Annotated[list, operator.add]` reducer for fan-in | `state.py` — `agent_results` field |
| Send API for parallel fan-out | `nodes.py` — `fan_out_node` |
| Compiled sub-graphs as nodes | `graph.py` — `billing_agent`, `technical_agent`, … |
| `ToolNode` for in-process tool execution | `agents/base_agent.py` |
| Conditional edges | `routing.py`, `agents/base_agent.py` |
| Reflection loop (agent → critic → agent) | `agents/base_agent.py` |
| Cycle in main graph (improve → fan_out retry) | `graph.py`, `routing.py` — `route_after_eval` |
| `interrupt()` for human-in-the-loop | `nodes.py` — `wait_for_human_node` |
| `AsyncPostgresSaver` checkpointer | `checkpointer.py` |
| `escalation_queue` table as async handoff | `store.py` — UI writes decisions; pipeline resumes |

## Setup

**1. Install dependencies**

```bash
cd agent-langgraph
pip install -r requirements.txt
```

`fastembed` will download the `all-MiniLM-L6-v2` model (~90 MB) on first use for knowledge base embeddings.

**2. Set environment variables**

```bash
cp .env.example .env
# edit .env — required:
#   ANTHROPIC_API_KEY=...
#   DATABASE_URL=postgresql://agents:agents@localhost:5432/agents_langgraph
```

**3. Start Postgres**

```bash
docker compose up -d
```

The database schema (tables, HNSW indexes, LangGraph checkpoint tables) is created automatically on first run. Seed data is loaded from `data/` if the tables are empty.

**4. Run the pipeline**

```bash
python pipeline.py --limit 3
```

**5. (Optional) Run the escalation review UI**

```bash
# Terminal 1 — pipeline in serve mode: processes emails then keeps polling for human decisions
python pipeline.py --limit 3 --serve

# Terminal 2 — FastAPI backend (port 8001)
cd ui/backend && uvicorn main:app --port 8001 --reload

# Terminal 3 — React frontend (port 5173)
cd ui/frontend && npm install && npm run dev
```

Open http://localhost:5173 to review and approve or override escalated tickets.

## Pipeline flags

```
python pipeline.py [options]

Core
  --limit N           emails to process (default: 3); use 0 for serve-only mode
  --offset N          skip first N emails (default: 0)
  --language LANG     filter by language: en | de (default: en)
  --shuffle           randomise email order

Safety
  --screen / --no-screen   run injection screener (default: on)

Eval  (default: on)
  --eval / --no-eval
  --save / --no-save       write eval_output.md (default: on)
  --internal-summary       include agent internal summaries

Improve  (default: on, requires --eval)
  --improve / --no-improve
  --apply / --no-apply     apply proposals to DB immediately (default: on)
  --min-score FLOAT        avg score threshold to trigger improve (default: 4.5)

Serve  (default: on)
  --serve / --no-serve     after the email loop, keep running and poll escalation_queue
                           every 5 s; auto-resumes interrupted threads when humans decide

Human-in-the-loop (manual)
  --resume THREAD_ID   resume a pipeline paused by an escalation interrupt
  --decision TEXT      human decision: "approve" or "override: <guidance>"
```

### Common invocations

```bash
# Pipeline only — no eval/improve
python pipeline.py --no-eval --limit 5

# Eval only — scores replies, writes eval_output.md
python pipeline.py --no-improve --limit 20

# Full cycle — eval + improve + apply to DB
python pipeline.py --limit 10

# Process emails then stay running to auto-resume escalations via UI
python pipeline.py --limit 3 --serve

# Serve-only: no new emails, just poll and resume pending escalations
python pipeline.py --limit 0 --serve

# Manual resume of an escalated ticket (without --serve)
python pipeline.py --resume email-42-3 --decision "approve"
python pipeline.py --resume email-42-3 --decision "override: Please process the refund without waiting for verification"
```

### Visualise the graph

LangGraph can render the compiled pipeline as a Mermaid diagram — something neither `agent-mcp` nor `agent-cli` can do:

```bash
python -c "
from graph import build_main_graph
g = build_main_graph()
print(g.get_graph().draw_mermaid())
" > graph.md
```

## How the improve loop works

Same as `agent-cli` and `agent-mcp` — after each email is scored, if the average is below `--min-score`, the improver analyses the skill used and proposes targeted changes.

**Proposal types**

| Type | When | What it changes |
|------|------|----------------|
| `kb_entry` | Ground truth contains a direct factual answer | Inserts or merges a versioned entry into `knowledge_base` |
| `agent_guideline` | Ground truth shows agent collecting info before acting | Inserts or merges a versioned entry into `agent_guidelines` |
| `skill_edit` | Eval comment identifies a workflow problem | Inserts a new active version of the skill |
| `new_skill` | Email type is entirely unhandled | Inserts a new skill row |

**Versioning** — updates are non-destructive. Old rows are set `is_active = false`; new rows get an incremented `version`. All versions are retained for audit and rollback.

## Project structure

```
agent-langgraph/
├── pipeline.py               # entry point: --limit, --eval, --improve, --serve, --resume
├── graph.py                  # main StateGraph — assembles all nodes and edges
├── state.py                  # PipelineState + AgentState TypedDicts with reducers
├── nodes.py                  # node functions: screen, classify, decompose, fan_out,
│                             #   wait_for_human, merge, eval, improve, wrap_agent_result
├── routing.py                # conditional edge functions: route_screen, route_after_merge,
│                             #   route_after_eval
├── tools.py                  # @tool decorated functions (ToolNode / in-process execution)
│                             #   replaces cli.py (subprocess) and mcp_server.py (HTTP)
├── checkpointer.py           # AsyncPostgresSaver for interrupt() state persistence
├── agents/
│   ├── base_agent.py         # builds StateGraph(AgentState): agent → tools → critic loop
│   ├── billing.py            # compiled billing sub-graph
│   ├── technical.py          # compiled technical_support sub-graph
│   ├── returns.py            # compiled returns sub-graph
│   └── general.py            # compiled general sub-graph
├── ui/
│   ├── backend/
│   │   ├── main.py           # FastAPI server (port 8001): GET /api/escalations,
│   │   │                     #   POST /api/escalations/{id}/decide → writes to escalation_queue
│   │   └── requirements.txt  # fastapi, uvicorn
│   └── frontend/             # React 18 + Vite 5 + TypeScript
│       ├── src/
│       │   ├── App.tsx        # polls /api/escalations every 5 s
│       │   ├── components/
│       │   │   ├── EscalationCard.tsx   # expandable card with Approve / Override buttons
│       │   │   ├── DecisionModal.tsx    # override text modal
│       │   │   └── ResolvedList.tsx     # collapsible history
│       │   └── types.ts       # Escalation TypeScript interface
│       └── vite.config.ts     # proxies /api → http://localhost:8001
├── # Copied from agent-cli (identical — no framework dependency):
├── classifier.py, client.py, email_stream.py, email_sanitizer.py
├── input_screener.py, evaluator.py, improver.py
├── logger.py, skills.py, store.py
├── docker-compose.yml        # pgvector/pgvector:pg17 on port 5432 (agents_langgraph DB)
└── data/                     # emails.csv, knowledge_base.json, agent_guidelines.json,
                              # training_set.json, skills/**/*.md (seeded on first run)
```

## Skills

Each skill `.md` file has a YAML frontmatter block and a system prompt body:

```yaml
---
name: process_refund
agent: billing
types: [Incident, Request]
tools: [lookup_customer, check_order_status, process_refund, send_reply]
---
You are a refund specialist...
```

`agent` must be one of `billing`, `returns`, `technical_support`, `general`. The `tools` list controls which `@tool` functions are passed to `bind_tools()` for that skill — the LLM only sees the tools it needs.

## Comparison with agent-mcp and agent-cli

| | agent-mcp | agent-cli | agent-langgraph |
|---|---|---|---|
| Tool transport | HTTP (MCP server) | subprocess (CLI) | in-process (`ToolNode`) |
| Orchestration | `asyncio.gather` | `asyncio.gather` | `StateGraph` + Send API |
| Routing | Python if/elif | Python if/elif | conditional edges |
| Reflection | none | none | critic node loop |
| Human-in-the-loop | none | none | `interrupt()` + escalation review UI |
| State management | dataclasses | dataclasses | `TypedDict` + reducers |
| Graph visualisation | none | none | `draw_mermaid()` |
| Checkpointing | none | none | `AsyncPostgresSaver` |
