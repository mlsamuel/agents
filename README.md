# Customer Support Agent System

A multi-agent customer support pipeline built with Claude and the Model Context Protocol (MCP). Emails are classified, routed to specialist workflow agents, and handled using skill files that drive tool selection and reply logic. An integrated eval+improve loop scores replies and automatically updates skills and the knowledge base.

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
    │   ├── lookup_customer
    │   ├── get_ticket_history
    │   ├── create_ticket
    │   ├── check_order_status
    │   ├── process_refund
    │   ├── escalate_to_human
    │   ├── send_reply
    │   ├── search_knowledge_base   ← pgvector ANN over knowledge_base table
    │   └── run_code (sandboxed Python)
    ▼
merged reply + WorkflowResult
    │
    ▼  (when --eval)
evaluator                     ← Haiku: scores action / completeness / tone
    │
    ▼  (when --improve and avg < --min-score)
improver                      ← Sonnet: proposes skill_edit / kb_entry / new_skill
    │                            applies to DB; pgvector similarity check before
    │                            inserting KB entries to prevent duplicates
    ▼
Postgres (skills + knowledge_base tables)
```

Skills are stored in the `skills` Postgres table (versioned, with `is_active` flag). The knowledge base lives in the `knowledge_base` table with a pgvector HNSW index. Both are seeded from `skills/<queue>/*.md` and `data/knowledge_base.json` on first run.

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
```

The database schema (tables, HNSW index) is created automatically on first run. Seed data is loaded from `data/knowledge_base.json` and `skills/**/*.md` if the tables are empty.

**3. Docker sandbox (optional)**

`run_code` executes agent-generated Python in a Docker container for isolation. Without Docker it falls back to in-process execution automatically.

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
  --min-score FLOAT        avg score threshold to trigger improve (default: 4.6)
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
| `kb_entry` | Ground truth contains facts the agent's reply was missing | Inserts or merges a versioned entry into the `knowledge_base` table |
| `skill_edit` | Eval comment identifies a workflow problem (wrong action, wrong tool) | Inserts a new active version of the skill into the `skills` table |
| `new_skill` | Email type is entirely unhandled by any existing skill | Inserts a new skill row into the `skills` table |

**KB deduplication** — before inserting a new KB entry, the improver runs a pgvector similarity search. If an existing active entry scores ≥ 0.90 cosine similarity, Haiku merges the two entries and creates a new version rather than inserting a duplicate.

**Versioning** — both skill and KB updates are non-destructive. The previous active row is set `is_active = false`; a new row with an incremented `version` is inserted. Old versions remain for audit and rollback.

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
├── kb.py                     # asyncpg pool, schema bootstrap, pgvector search/insert/upsert
├── skills.py                 # asyncpg pool, skill loading and versioning
├── logger.py                 # shared logging config
├── client.py                 # Anthropic client wrapper
├── skills/
│   ├── billing/
│   ├── general/
│   ├── returns/
│   └── technical_support/   # seed source — loaded to DB on first run
└── data/
    ├── emails.csv            # email dataset (subject, body, answer, type, queue, priority, language)
    └── knowledge_base.json  # seed source — loaded to knowledge_base table on first run
```

## Skills

Each skill `.md` file (seed source) has a YAML frontmatter block and a system prompt body:

```yaml
---
name: process_refund
queue: billing
types: [refund, return, billing_dispute]
tools: [lookup_customer, check_order_status, process_refund, send_reply]
---
You are a refund specialist...
```

At runtime, skills are read from the `skills` Postgres table. The `tools` list controls which MCP tools the agent can access. The improver can update skills in-place (new versioned row, old row deactivated) without touching the seed files.
