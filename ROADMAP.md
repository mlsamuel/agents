# Technical Debt Audit & Improvement Roadmap

## Shortcuts taken

### Data persistence

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| D1 | **All backend data is mocked** | `mcp_server.py` generates deterministic fake customers/tickets/orders from hashed keywords. | Nothing persists. No real support history. |
| D4 | **Generic embedding model** | `fastembed` (`all-MiniLM-L6-v2`) used for KB and guideline search. | Lower retrieval quality than purpose-built models (VoyageAI, Cohere, OpenAI). |

### Improve agent gaps

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| I1 | **Can't propose new MCP tools** | `IMPROVE_SYSTEM` only knows `skill_edit`, `kb_entry`, `new_skill`. | If an email fails because a needed tool doesn't exist, rewriting the skill won't help. |
| I2 | **No baseline / experiment tracking** | Eval scores written to `eval_output.md` but no history across runs. | Can't detect gradual drift or measure cumulative improvement. |

### Architecture

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| A2 | **Max tool turns is a global constant** | `MAX_TOOL_TURNS = 8` hardcoded. | Can't tune per skill, no adaptive termination. |
| A4 | **No cost tracking** | No visibility into per-run API spend. | Can't budget or optimize model selection. |

---

## Prioritized upgrade plan

### Phase 1 — Data persistence

**1a. Postgres for customers, tickets, orders** — *fixes D1*
- Schema: `customers`, `tickets`, `ticket_events`, `orders`, `refunds`
- Replace mocked tools with real `asyncpg` queries — MCP tool API surface unchanged
- New file: `db.py` (connection pool, schema bootstrap, query helpers)
- Updated file: `mcp_server.py`

---

### Phase 2 — Improve agent completeness

**2b. `new_tool` proposal type** — *fixes I1*
- Add a tool registry to `mcp_server.py` (tool names, signatures, descriptions)
- Include tool registry in the improver prompt context
- Detection rule in `IMPROVE_SYSTEM`: if eval comment says agent couldn't do X and no tool covers X → propose `new_tool`
- Proposal: `{ "type": "new_tool", "tool_name": "...", "parameters": {...}, "rationale": "...", "implementation_sketch": "..." }`
- `--apply`: writes a stub to `mcp_server.py` with `raise NotImplementedError`; human completes it
- Updated files: `improver.py`, `mcp_server.py`

**2c. Experiment log** — *fixes I2*
- `improvement_log.jsonl` (gitignored): one JSON line per run — `{ timestamp, run_id, skill_versions, before_avg, after_avg, delta, emails_n }`
- `evaluator.py` appends a baseline entry on every `--save` run
- `improver.py` reads last baseline and prints cumulative improvement trend
- Updated files: `evaluator.py`, `improver.py`

---

### Phase 3 — Architecture cleanup

**3a. Cost tracking** — *fixes A4*
- Instrument `Client._Messages.create()` to extract `usage.input_tokens + output_tokens`
- Compute cost using a model pricing table in `client.py`
- Print per-run cost summary at end of pipeline run
- Updated file: `client.py`

---

## Sequencing

```
Next      Phase 1a  Postgres for backend data (customers, tickets, orders)

Month 1   Phase 2   Improve agent: new_tool proposals, git integration, experiment log

Ongoing   Phase 3a  Cost tracking
```

---

## File impact summary

| File | What changes |
|------|-------------|
| `mcp_server.py` | Phase 1a: Postgres queries; Phase 2b: tool registry |
| `improver.py` | Phase 2b: new_tool proposals; Phase 2c: experiment log |
| `pipeline.py` | Phase 2c: experiment log |
| `evaluator.py` | Phase 2c: experiment log baseline |
| `client.py` | Phase 3a: cost tracking |
| NEW `db.py` | Phase 1a: asyncpg pool, schema, query helpers for customers/tickets/orders |
