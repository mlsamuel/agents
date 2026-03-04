# Technical Debt Audit & Improvement Roadmap

## Shortcuts taken

### Security

| # | Shortcut | Current state | Risk |
|---|----------|--------------|------|
| S1 | **Code sandbox is AST-only** | `run_code` uses AST checks + restricted builtins + SIGALRM (Unix-only). No OS-level isolation. | Crafted snippets can escape. Fails on Windows. |
| S2 | **Injection detection is regex** | `email_sanitizer.py` strips keyword patterns. Bypassed trivially with unicode spacing, obfuscation, or semantic rewording. | Determined attackers will succeed. |
| S3 | **LLM-based skill selection** | Haiku picks the skill from the email subject; fallback is `skills[0]`. | Adversarial subjects can influence routing. |

### Data persistence

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| D1 | **All backend data is mocked** | `mcp_server.py` generates deterministic fake customers/tickets/orders from hashed keywords. | Nothing persists. No real support history. |
| D2 | **Skills as flat .md files** | YAML frontmatter + body, read from disk. No versioning, no schema validation. | Can't roll back a bad improve run. No audit trail. |
| D3 | **KB as a flat JSON file** | Loaded into memory, rewritten in full on every update. | Doesn't scale. No versioning. Concurrent writes corrupt it. |
| D4 | **Embeddings computed in-process** | `SentenceTransformer("all-MiniLM-L6-v2")` runs inside the MCP server. Reloads on every subprocess spawn. | Slow cold start per request. Lower retrieval quality than purpose-built models. |

### Improve agent gaps

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| I1 | **Can't propose new MCP tools** | `IMPROVE_SYSTEM` only knows `skill_edit`, `kb_entry`, `new_skill`. | If an email fails because a needed tool doesn't exist, rewriting the skill won't help. |
| I2 | **No regression testing** | `--apply` re-evals only the *failing* emails. | A skill change that fixes email #3 can break email #7 — undetected. |
| I3 | **Proposals applied directly to disk** | `_apply_proposals()` writes files immediately, no commit, no approval gate, no rollback. | A bad proposal permanently overwrites a skill. |
| I4 | **No baseline / experiment tracking** | Eval scores written to `eval_results.json` but no history across runs. | Can't detect gradual drift or measure cumulative improvement. |

### Architecture

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| A1 | **New MCP subprocess per request** | `workflow_agent.py` spawns a fresh `mcp_server.py` process per email. KB reloads every time. | High latency, wasted compute, no connection pooling. |
| A2 | **Max tool turns is a global constant** | `MAX_TOOL_TURNS = 8` hardcoded. | Can't tune per skill, no adaptive termination. |
| A3 | **Email body hard-truncated at 2000 chars** | Simple slice in `workflow_agent.py`. | Long technical emails lose critical context. |
| A4 | **No cost tracking** | No visibility into per-run API spend. | Can't budget or optimize model selection. |

---

## Prioritized upgrade plan

### Phase 1 — Security *(blocks production use)*

**1a. Docker sandbox for `run_code`** — *fixes S1*
- Replace SIGALRM + AST approach with a Docker container per `run_code` invocation
- `python:3.12-slim`, `--network none`, read-only rootfs, `--memory 128m`, `--cpus 0.5`
- MCP server passes code + namespace bindings via stdin; captures stdout; timeout via `--stop-timeout`
- Files: `mcp_server.py`

**1b. Make `input_screener` the primary injection defense** — *mitigates S2*
- Change `--screen` default to `True` in `pipeline.py` and `eval_agent.py`
- Keep regex sanitizer as a cheap pre-filter only, not the security boundary
- Files: `pipeline.py`, `eval_agent.py`

---

### Phase 2 — Data persistence

**2a. Postgres for customers, tickets, orders** — *fixes D1*
- Schema: `customers`, `tickets`, `ticket_events`, `orders`, `refunds`
- Replace mocked tools with real `asyncpg` queries — MCP tool API surface unchanged
- New file: `db.py` (connection pool, schema bootstrap, query helpers)
- Updated file: `mcp_server.py`

**2b. Postgres + pgvector + VoyageAI for the knowledge base** — *fixes D3, D4*
- Move `data/knowledge_base.json` → `knowledge_base` Postgres table with a `pgvector` embedding column
- Replace `SentenceTransformer` with **VoyageAI** (`voyage-3-lite` / `voyage-3`)
  - Embeddings computed at insert time via VoyageAI API; stored in pgvector
  - `search_knowledge_base` issues an ANN query instead of numpy dot product
  - No model loaded in-process → fast MCP server cold start
- `improve_agent` KB proposals: embed via VoyageAI → INSERT row (no JSON rewrite)
- New file: `embeddings.py` (VoyageAI client wrapper)
- Updated files: `mcp_server.py`, `improve_agent.py`

**2c. Postgres for skills** — *fixes D2*
- Table: `skills(id, name, queue, types[], tools[], system_prompt, version, active, created_at)`
- `load_skills()` in `workflow_agent.py` → DB query filtered to `active = true`
- `improve_agent` skill proposals: INSERT new version row, deactivate old → full history, instant rollback
- Seed from existing `.md` files on first run
- Updated files: `workflow_agent.py`, `improve_agent.py`

---

### Phase 3 — Improve agent completeness

**3a. Regression testing** — *fixes I2*
- After `--apply`, re-eval **all** emails for the affected skill(s), not just the failing ones
- If any previously-passing email drops > 0.5 avg points: warn and require `--force`
- Flag: `--regression` (default on when `--apply` is used)
- Updated file: `improve_agent.py`

**3b. `new_tool` proposal type** — *fixes I1*
- Add a tool registry to `mcp_server.py` (tool names, signatures, descriptions)
- Include tool registry in the improve_agent prompt context
- Detection rule in `IMPROVE_SYSTEM`: if eval comment says agent couldn't do X and no tool covers X → propose `new_tool`
- Proposal: `{ "type": "new_tool", "tool_name": "...", "parameters": {...}, "rationale": "...", "implementation_sketch": "..." }`
- `--apply`: writes a stub to `mcp_server.py` with `raise NotImplementedError`; human completes it
- Updated files: `improve_agent.py`, `mcp_server.py`

**3c. Git-integrated proposals** — *fixes I3*
- `_apply_proposals()` creates a branch `improve/<timestamp>`, applies changes, commits with link to eval run
- Dry-run prints a diff and requires confirmation before committing
- `--no-branch`: apply directly to working tree (opt-in, current behaviour)
- Updated file: `improve_agent.py`

**3d. Experiment log** — *fixes I4*
- `improvement_log.jsonl` (gitignored): one JSON line per run — `{ timestamp, run_id, skill_versions, before_avg, after_avg, delta, emails_n }`
- `eval_agent.py` appends a baseline entry on every `--save` run
- `improve_agent.py` reads last baseline and prints cumulative improvement trend
- Updated files: `eval_agent.py`, `improve_agent.py`

---

### Phase 4 — Architecture cleanup

**4a. Long-lived MCP server** — *fixes A1*
- Move MCP transport from stdio subprocess → HTTP (SSE or streamable-HTTP)
- MCP server runs as a persistent service; `workflow_agent.py` connects as a client
- Eliminates per-request subprocess spawn, KB reload, embedding model init
- Updated files: `mcp_server.py`, `workflow_agent.py`

**4b. Cost tracking** — *fixes A4*
- Instrument `Client._Messages.create()` to extract `usage.input_tokens + output_tokens`
- Compute cost using a model pricing table in `client.py`
- Print per-run cost summary at end of pipeline/eval/improve runs
- Updated file: `client.py`

**4c. Structured logging**
- `LOG_LEVEL` env var replaces hardcoded DEBUG (default `INFO`)
- `RotatingFileHandler` (`agents.log`, 10 MB, 3 backups)
- Updated file: `logger.py`

---

## Sequencing

```
Month 1   Phase 1   Docker sandbox, screener default-on
          Phase 2a  Postgres for backend data (customers, tickets, orders)

Month 2   Phase 2b  VoyageAI + pgvector for knowledge base
          Phase 2c  Skills in Postgres

Month 3   Phase 3   Improve agent: regression testing, new_tool proposals,
                    git integration, experiment log

Month 4   Phase 4a  Long-lived MCP server (HTTP transport)
Ongoing   Phase 4bc Cost tracking, structured logging
```

---

## File impact summary

| File | What changes |
|------|-------------|
| `mcp_server.py` | Docker exec for `run_code`; Postgres queries; tool registry; VoyageAI + pgvector KB |
| `workflow_agent.py` | `load_skills()` → DB; HTTP MCP client |
| `improve_agent.py` | `new_tool` proposals; regression gate; git branch workflow; experiment log |
| `eval_agent.py` | Experiment log baseline; cost tracking |
| `client.py` | Cost tracking |
| `logger.py` | `LOG_LEVEL` env var; `RotatingFileHandler` |
| `pipeline.py` | `--screen` default → `True` |
| NEW `db.py` | asyncpg pool, schema, query helpers |
| NEW `embeddings.py` | VoyageAI client wrapper |
