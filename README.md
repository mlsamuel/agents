# Customer Support Agent System

A multi-agent customer support pipeline built with Claude and the Model Context Protocol (MCP). Emails are classified, routed to specialist workflow agents, and handled using skill files that drive tool selection and reply logic. An integrated eval+improve loop scores replies and automatically updates skills, the knowledge base, and agent guidelines.

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
workflow_agent(s)             ← Sonnet + MCP tools via skill files (from DB)
    │   ├── lookup_customer           connects to persistent mcp_server.py
    │   ├── get_ticket_history        over streamable-HTTP (one process per
    │   ├── create_ticket             pipeline run, shared across all emails)
    │   ├── check_order_status
    │   ├── process_refund
    │   ├── escalate_to_human
    │   ├── send_reply
    │   ├── search_knowledge_base     ← pgvector ANN over knowledge_base table
    │   ├── search_agent_guidelines   ← pgvector ANN over agent_guidelines table
    │   └── run_code (sandboxed Python)
    ▼
merged reply + WorkflowResult
    │
    ▼  (when --eval)
evaluator                     ← Haiku: scores action / completeness / tone
    │
    ▼  (when --improve and avg < --min-score)
improver                      ← Sonnet: proposes skill_edit / kb_entry /
    │                            agent_guideline / new_skill
    │                            pgvector similarity check before insert/merge
    │                            regression test on training_set after apply
    ▼
Postgres (skills + knowledge_base + agent_guidelines + training_set tables)
```

Skills are stored in the `skills` Postgres table (versioned, with `is_active` flag). The knowledge base lives in the `knowledge_base` table and agent-facing handling patterns in `agent_guidelines`, both with pgvector HNSW indexes. A `training_set` table holds up to 3 golden emails per skill for regression testing after each apply.

All tables are seeded from `data/` on first run: `skills/**/*.md`, `data/knowledge_base.json`, `data/agent_guidelines.json`, `data/training_set.json`.

`pipeline.py` starts `mcp_server.py` as a persistent HTTP subprocess at startup (port 8765) and terminates it when the run finishes. All workflow agents in a run share the same server process, so the embedding model and DB connection pool are initialised once.

## Setup

**1. Clone and install dependencies**

```bash
git clone <repo-url>
cd agents
pip install -r requirements.txt
```

`fastembed` will download the `all-MiniLM-L6-v2` model (~90 MB) on first use for knowledge base embeddings.

**2. Set environment variables**

```bash
cp .env.example .env
# edit .env — required:
#   ANTHROPIC_API_KEY=...
#   DATABASE_URL=postgresql://user:pass@host/dbname
#
# optional (defaults shown):
#   MCP_PORT=8765          — port the MCP server binds to
#   MCP_SERVER_URL=http://127.0.0.1:8765/mcp  — URL workflow agents connect to
```

The database schema (tables, HNSW indexes) is created automatically on first run. Seed data is loaded from `data/` if the tables are empty.

**3. Docker sandbox (required for `run_code`)**

`run_code` executes agent-generated Python in a Docker container for isolation. Without Docker, `run_code` calls return an error and the agent cannot use that tool.

```bash
docker pull python:3.12-slim
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

### How the improve loop works

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

**Regression testing** — after each `--apply`, the training emails for the affected skill are re-evaluated (classify → orchestrate → judge). Any email scoring below avg 3.5 triggers a warning. The failing email is also offered to the training set (up to 3 emails per skill).

**EVAL SUMMARY** — printed at the end of each run; includes per-dimension scores plus a count of skills edited, KB entries added, guidelines added, and training emails added during the run.

## Project structure

```
agents/
├── pipeline.py               # unified entry point: screen → classify → orchestrate → eval → improve
├── classifier.py             # email classifier (Haiku)
├── orchestrator_agent.py     # decomposes + fans out to workflow agents
├── workflow_agent.py         # skill-based tool-use loop (Sonnet + MCP)
├── mcp_server.py             # FastMCP server — all support backend tools
├── email_stream.py           # reads data/emails.csv
├── email_sanitizer.py        # pattern-based injection strip
├── input_screener.py         # LLM-based injection detector
├── evaluator.py              # LLM-as-judge scoring (judge, append_section)
├── improver.py               # eval-driven skill/KB improvement (generate_proposals, apply_proposals)
├── store.py                  # asyncpg pool, schema bootstrap, pgvector search/insert/upsert
│                             #   (knowledge_base + agent_guidelines + training_set)
├── skills.py                 # asyncpg pool, skill loading and versioning
├── logger.py                 # shared logging config
├── client.py                 # Anthropic client wrapper
├── data/skills/
│   ├── billing/
│   ├── general/
│   ├── returns/
│   └── technical_support/   # seed source — loaded to DB on first run
└── data/
    ├── emails.csv            # email dataset (subject, body, answer, type, queue, priority, language)
    ├── knowledge_base.json   # seed source — loaded to knowledge_base table on first run
    ├── agent_guidelines.json # seed source — loaded to agent_guidelines table on first run
    └── training_set.json     # seed source — loaded to training_set table on first run
```

## Skills

Each skill `.md` file (seed source) has a YAML frontmatter block and a system prompt body:

```yaml
---
name: process_refund
queue: billing
types: [Incident, Request]
tools: [lookup_customer, check_order_status, process_refund, send_reply]
---
You are a refund specialist...
```

The directory name (`billing/`, `returns/`, etc.) is used as the queue key — the `queue:` frontmatter field is documentation only. At runtime, skills are read from the `skills` Postgres table. The `tools` list controls which MCP tools the agent can access. The improver can update skills in-place (new versioned row, old row deactivated) without touching the seed files.
