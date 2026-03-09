# Customer Support Agent System — CLI Edition

A multi-agent customer support pipeline built with Claude and a CLI tool interface. Emails are classified, routed to specialist workflow agents, and handled using skill files that drive tool selection and reply logic. An integrated eval+improve loop scores replies and automatically updates skills, the knowledge base, and agent guidelines. Every eval run is persisted to Postgres and can be viewed as a self-contained HTML showcase.

This project is a variant of `agent-mcp` that replaces the MCP server with a CLI interface: instead of connecting to a protocol server over HTTP, workflow agents invoke `cli.py` as a subprocess and parse its structured JSON output — the same pattern used by [googleworkspace/cli](https://github.com/googleworkspace/cli).

## Architecture

```
email_stream
    │
    ▼
input_screener (optional)     ← Haiku: prompt injection detection
    │
email_sanitizer               ← pattern-based strip of injection attempts
    │
    ▼
classifier                    ← Haiku: queue / type / priority
    │
    ▼
orchestrator_agent            ← Sonnet: decomposes multi-topic emails,
    │                            fans out to parallel WorkflowAgents
    ▼
workflow_agent(s)             ← Sonnet + CLI tools via skill files (from DB)
    │   ├── lookup_customer           invokes cli.py as a subprocess
    │   ├── get_ticket_history        each call returns structured JSON
    │   ├── create_ticket             no server process required
    │   ├── check_order_status
    │   ├── process_refund
    │   ├── escalate_to_human
    │   ├── send_reply
    │   ├── search_knowledge_base     ← pgvector ANN over knowledge_base table
    │   └── search_agent_guidelines   ← pgvector ANN over agent_guidelines table
    ▼
merged reply + WorkflowResult
    │
    ▼  (when --eval)
evaluator                     ← Haiku: scores action / completeness / tone
    │                            results written to pipeline_results table
    ▼  (when --improve and avg < --min-score)
improver                      ← Sonnet: proposes skill_edit / kb_entry /
    │                            agent_guideline / new_skill
    │                            pgvector similarity check before insert/merge
    │                            regression test on training_set after apply
    ▼
Postgres (skills + knowledge_base + agent_guidelines + training_set +
          pipeline_runs + pipeline_results tables)
```

### CLI tool interface

Tools are defined in `tool_registry.py` (single source of truth) and exposed as CLI commands via `cli.py`:

```
python cli.py <namespace> <command> [--flags]
```

| Namespace | Commands |
|-----------|----------|
| `crm`     | `lookup-customer`, `ticket-history` |
| `orders`  | `check-status`, `process-refund` |
| `tickets` | `create` |
| `comms`   | `send-reply`, `escalate` |
| `kb`      | `search`, `guidelines` |

Every command outputs structured JSON to stdout. See [SKILL.md](SKILL.md) for the full command reference with example inputs and outputs.

When Claude calls a tool, `workflow_agent.py` maps the tool name to a CLI command using `tool_registry.BY_NAME`, runs it via `subprocess.run`, and feeds the JSON stdout back as the tool result — no sockets, no protocol server.

## Setup

**1. Install dependencies**

```bash
cd agent-cli
pip install -r requirements.txt
```

`fastembed` will download the `all-MiniLM-L6-v2` model (~90 MB) on first use for knowledge base embeddings.

**2. Set environment variables**

```bash
cp .env.example .env
# edit .env — required:
#   ANTHROPIC_API_KEY=...
#   DATABASE_URL=postgresql://user:pass@host/dbname
```

The database schema (tables, HNSW indexes) is created automatically on first run. Seed data is loaded from `data/` if the tables are empty.

**3. Start Postgres**

```bash
docker-compose up -d
```

**4. Run the pipeline**

```bash
python pipeline.py --limit 3
```

## Pipeline flags

```
python pipeline.py [options]

Core
  --limit N           emails to process (default: 3)
  --offset N          skip first N emails (default: 0)
  --language LANG     filter by language: en | de (default: en)
  --shuffle           randomise email order

Safety
  --screen / --no-screen   run injection screener (default: on)

Eval  (default: on)
  --eval / --no-eval
  --save / --no-save       write eval_output.md, appended per email (default: on)
  --internal-summary       include agent internal summaries in eval_output.md

Improve  (default: on, requires --eval)
  --improve / --no-improve
  --apply / --no-apply     apply proposals to DB immediately (default: on)
  --min-score FLOAT        avg score threshold to trigger improve (default: 4.5)
```

### Common invocations

```bash
# Pipeline only — no eval/improve
python pipeline.py --no-eval --limit 5

# Eval only — scores replies, writes eval_output.md
python pipeline.py --no-improve --limit 20

# Full cycle — eval + improve + apply to DB
python pipeline.py --limit 10

# Dry run — see proposals without applying
python pipeline.py --no-apply --limit 5
```

### Test CLI tools directly

```bash
python cli.py crm lookup-customer --keyword "Jane Smith"
python cli.py orders check-status --order-ref ORD-00123456
python cli.py kb search --query "refund policy" --category billing
```

## Showcase UI

Every pipeline eval run is automatically stored in Postgres (`pipeline_runs` + `pipeline_results` tables). You can view results three ways:

### Static HTML (no servers needed)

```bash
python ui/export_showcase.py            # latest run → ui/showcase/index.html
python ui/export_showcase.py --run 3   # specific run id
open ui/showcase/index.html
```

Generates a fully self-contained file with data, CSS, and JS inlined — open it with a double-click in any browser. The file at `ui/showcase/index.html` is committed to the repo.

### Live UI (React + FastAPI)

```bash
# Terminal 1 — backend
cd ui/backend && pip install -r requirements.txt
uvicorn main:app --port 8000 --reload

# Terminal 2 — frontend (requires Node 18+)
cd ui/frontend && npm install && npm run dev
```

Open http://localhost:5173 — run selector dropdown, expandable cards, side-by-side ground truth vs generated reply, colour-coded scores.

## How the improve loop works

After each email is scored, if the average is below `--min-score`, the improver analyses the skill used and proposes targeted changes.

**Proposal types**

| Type | When | What it changes |
|------|------|----------------|
| `kb_entry` | Ground truth contains a direct factual answer (policy, price, procedure) | Inserts or merges a versioned customer-facing entry into `knowledge_base` |
| `agent_guideline` | Ground truth shows the agent collecting info before acting (account numbers, dates, platform details) | Inserts or merges a versioned agent-facing entry into `agent_guidelines` |
| `skill_edit` | Eval comment identifies a workflow problem (wrong action, wrong tool) | Inserts a new active version of the skill into the `skills` table |
| `new_skill` | Email type is entirely unhandled by any existing skill | Inserts a new skill row into the `skills` table |

**Deduplication** — before inserting a new KB or guideline entry, the improver runs a pgvector similarity search. If an existing active entry scores ≥ 0.90 cosine similarity, Haiku merges the two entries and creates a new version rather than inserting a duplicate.

**Versioning** — skill, KB, and guideline updates are non-destructive. The previous active row is set `is_active = false`; a new row with an incremented `version` is inserted. Old versions remain for audit and rollback.

**Regression testing** — after each `--apply`, the training emails for the affected skill are re-evaluated (classify → orchestrate → judge). If any email scores below avg 3.5 and the proposal was a `skill_edit`, the new skill version is automatically deactivated and the previous version restored. The current email is also added to the training set if there is room (up to 3 per skill).

## Project structure

```
agent-cli/
├── pipeline.py               # unified entry point: screen → classify → orchestrate → eval → improve
│                             #   stores every eval run to pipeline_runs + pipeline_results
├── cli.py                    # Click CLI — exposes all tools as JSON-output commands
├── tool_registry.py          # single source of truth: tool name → CLI routing + Anthropic schema
├── tools.py                  # pure tool implementations (no framework dependency)
├── SKILL.md                  # CLI command reference for agents and humans
├── classifier.py             # email classifier (Haiku)
├── orchestrator_agent.py     # decomposes + fans out to workflow agents
├── workflow_agent.py         # skill-based tool-use loop (Sonnet + CLI subprocess)
├── email_stream.py           # reads data/emails.csv
├── email_sanitizer.py        # pattern-based injection strip
├── input_screener.py         # LLM-based injection detector
├── evaluator.py              # LLM-as-judge scoring (judge, append_section)
├── improver.py               # eval-driven skill/KB improvement (generate_proposals, apply_proposals)
├── store.py                  # asyncpg pool, schema bootstrap, pgvector search/insert/upsert
│                             #   tables: knowledge_base, agent_guidelines, training_set,
│                             #           pipeline_runs, pipeline_results
├── skills.py                 # asyncpg pool, skill loading and versioning
├── logger.py                 # shared logging config
├── client.py                 # Anthropic client wrapper with retry and per-model cost tracking
├── docker-compose.yml        # Postgres + pgvector
├── data/skills/
│   ├── billing/
│   ├── general/
│   ├── returns/
│   └── technical_support/   # seed source — loaded to DB on first run
│                             #   NOTE: DB is the live source; seed files reflect the latest
│                             #   active version but may lag behind DB after improve runs
└── data/
    ├── emails.csv            # email dataset (subject, body, answer, type, queue, priority, language)
    ├── knowledge_base.json   # seed source — loaded to knowledge_base table on first run
    ├── agent_guidelines.json # seed source — loaded to agent_guidelines table on first run
    └── training_set.json     # seed source — loaded to training_set table on first run
ui/
├── export_showcase.py        # queries DB → writes self-contained ui/showcase/index.html
├── showcase/
│   └── index.html            # committed static showcase (regenerate with export_showcase.py)
├── backend/
│   ├── main.py               # FastAPI: GET /api/runs, GET /api/runs/{id}/results
│   └── requirements.txt
└── frontend/                 # React 18 + Vite + TypeScript
    ├── src/
    │   ├── App.tsx
    │   ├── types.ts
    │   └── components/
    │       ├── RunSelector.tsx
    │       └── ResultCard.tsx
    ├── package.json
    └── vite.config.ts        # proxies /api → localhost:8000
```

## Skills

Each skill `.md` file (seed source) has a YAML frontmatter block and a system prompt body:

```yaml
---
name: process_refund
agent: billing
types: [Incident, Request]
tools: [lookup_customer, check_order_status, process_refund, send_reply]
---
You are a refund specialist...
```

`agent` must be one of `billing`, `returns`, `technical_support`, `general`. `types` and `tools` are stored in the DB and used for routing and tool filtering. The improver can update skills in-place (new versioned row, old row deactivated) without touching the seed files.

## Adding or changing tools

All tool definitions live in `tool_registry.py`. Each entry is a dict with routing fields (`namespace`, `cli_command`, `params`) and the Anthropic tool schema (`description`, `input_schema`). Derived views (`BY_NAME`, `BY_NAMESPACE`, `SCHEMAS`) are imported by `workflow_agent.py`, `cli.py`, and the Docker sandbox runner — no other files need updating.

To add a tool:
1. Add a dict to `TOOLS` in `tool_registry.py`
2. Add the Click command to `cli.py`
3. Implement the logic in `tools.py`
