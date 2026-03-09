# Technical Debt Audit & Improvement Roadmap

## Shortcuts taken

### Data persistence

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| D1 | **All backend data is mocked** | `tools.py` generates deterministic fake customers/tickets/orders from hashed keywords. | Nothing persists. No real support history. |
| D4 | **Generic embedding model** | `fastembed` (`all-MiniLM-L6-v2`) used for KB and guideline search. | Lower retrieval quality than purpose-built models (VoyageAI, Cohere, OpenAI). |

### Improve agent gaps

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| I1 | **Can't propose new CLI tools** | `IMPROVE_SYSTEM` only knows `skill_edit`, `kb_entry`, `new_skill`. | If an email fails because a needed tool doesn't exist, rewriting the skill won't help. |
| I2 | **No baseline / experiment tracking** | Eval scores written to `eval_output.md` but no history across runs. | Can't detect gradual drift or measure cumulative improvement. |

### Architecture

| # | Shortcut | Current state | Consequence |
|---|----------|--------------|-------------|
| A1 | **CLI subprocess per tool call** | Each tool call spawns a new `python cli.py` process. | Cold-start overhead per call; a long-running CLI daemon or direct function calls would be faster. |
| A2 | **Max tool turns is a global constant** | `MAX_TOOL_TURNS = 8` hardcoded. | Can't tune per skill, no adaptive termination. |

---

## Prioritized upgrade plan

### Phase 1 — Data persistence

**1a. Postgres for customers, tickets, orders** — *fixes D1*
- Schema: `customers`, `tickets`, `ticket_events`, `orders`, `refunds`
- Replace mocked tools in `tools.py` with real `asyncpg` queries — CLI API surface unchanged
- New file: `db.py` (connection pool, schema bootstrap, query helpers)
- Updated file: `tools.py`

---

### Phase 2 — Improve agent completeness

**2b. `new_tool` proposal type** — *fixes I1*
- Add a tool registry to `cli.py` (tool names, CLI signatures, descriptions)
- Include tool registry in the improver prompt context
- Detection rule in `IMPROVE_SYSTEM`: if eval comment says agent couldn't do X and no tool covers X → propose `new_tool`
- Proposal: `{ "type": "new_tool", "tool_name": "...", "namespace": "...", "flags": [...], "rationale": "...", "implementation_sketch": "..." }`
- `--apply`: adds a stub command to `cli.py` and `tools.py` with `raise NotImplementedError`; human completes it
- Updated files: `improver.py`, `cli.py`, `tools.py`

**2c. Experiment log** — *fixes I2*
- `improvement_log.jsonl` (gitignored): one JSON line per run — `{ timestamp, run_id, skill_versions, before_avg, after_avg, delta, emails_n }`
- `evaluator.py` appends a baseline entry on every `--save` run
- `improver.py` reads last baseline and prints cumulative improvement trend
- Updated files: `evaluator.py`, `improver.py`

---

## Sequencing

```
Next      Phase 1a  Postgres for backend data (customers, tickets, orders)

Month 1   Phase 2   Improve agent: new_tool proposals, experiment log
```

---

## File impact summary

| File | What changes |
|------|-------------|
| `tools.py` | Phase 1a: Postgres queries; Phase 2b: new tool stubs |
| `cli.py` | Phase 2b: new command stubs from new_tool proposals |
| `improver.py` | Phase 2b: new_tool proposals; Phase 2c: experiment log |
| `pipeline.py` | Phase 2c: experiment log |
| `evaluator.py` | Phase 2c: experiment log baseline |
| NEW `db.py` | Phase 1a: asyncpg pool, schema, query helpers for customers/tickets/orders |
