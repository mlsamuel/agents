# Customer Support Agent System

A multi-agent customer support pipeline built with Claude and the Model Context Protocol (MCP). Emails are classified, routed to specialist workflow agents, and handled using skill files that drive tool selection and reply logic.

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
classifier_agent              ← Haiku: queue / type / priority
    │
    ▼
orchestrator_agent            ← Sonnet: decomposes multi-topic emails,
    │                            fans out to parallel WorkflowAgents
    ▼
workflow_agent(s)             ← Sonnet + MCP tools via skill .md files
    │   ├── lookup_customer
    │   ├── get_ticket_history
    │   ├── create_ticket
    │   ├── check_order_status
    │   ├── process_refund
    │   ├── escalate_to_human
    │   ├── send_reply
    │   ├── search_knowledge_base
    │   └── run_code (sandboxed Python)
    ▼
merged reply + WorkflowResult
```

Skills live in `skills/<agent_key>/*.md` (YAML frontmatter + system prompt). The improve pipeline can automatically update skills and the knowledge base based on eval scores.

## Setup

**1. Clone and install dependencies**

```bash
git clone <repo-url>
cd agents
pip install -r requirements.txt
```

`sentence-transformers` will download the `all-MiniLM-L6-v2` model (~90 MB) on first use for knowledge base search.

**2. Set your API key**

```bash
cp .env.example .env
# edit .env and add your Anthropic API key
```

**3. Run the pipeline**

```bash
python pipeline.py --limit 3
```

## Scripts

| Script | Purpose | Key flags |
|--------|---------|-----------|
| `pipeline.py` | End-to-end: stream → classify → route → reply | `--limit N`, `--language en\|de`, `--screen` |
| `classifier_agent.py` | Classify emails only | `--limit N` |
| `eval_agent.py` | Run eval and score replies against ground truth | `--limit N`, `--offset N`, `--screen`, `--save` |
| `improve_agent.py` | Propose and apply skill/KB improvements from eval results | `--min-score 4.0`, `--apply` |

### Eval + improve loop

```bash
# 1. Run eval — scores replies against ground truth, writes eval_results.json + eval_output.md
python eval_agent.py --limit 20 --save

# 2. Inspect proposals without changing anything (dry run)
python improve_agent.py --min-score 4.0

# 3. Apply improvements + re-evaluate the same emails to measure the delta
python improve_agent.py --min-score 4.0 --apply
```

### How the improve agent works

`improve_agent.py` reads `eval_results.json`, filters to emails where the average score is below `--min-score`, groups them by skill, then calls Claude Sonnet once per skill group to propose targeted improvements.

**Proposal types**

| Type | When | What it changes |
|------|------|----------------|
| `kb_entry` | Ground truth contains specific facts the agent's reply was missing (policies, pricing, procedures, deadlines) | Appends a new entry to `data/knowledge_base.json` |
| `skill_edit` | Eval comment describes a *workflow* problem (wrong action taken, wrong tool used, wrong order of steps) | Rewrites the skill `.md` file in place |
| `new_skill` | Email type is entirely unhandled by any existing skill | Creates a new `.md` file under `skills/<queue>/` |

The distinction matters: a missing fact is a knowledge base problem, not a skill problem. `skill_edit` is only proposed when the eval comment explicitly identifies a process or workflow gap.

**Decision rules applied by the LLM**
1. `kb_entry` — always proposed when ground truth has concrete info the agent omitted
2. `skill_edit` — only when the comment says the agent took the wrong action or followed the wrong process
3. `new_skill` — rare; only when no existing skill covers the email type at all

**With `--apply`**, proposals are written to disk and the same emails are re-evaluated. A before/after score table is printed and `eval_results.json` / `eval_output.md` are updated with the new scores.

## Project structure

```
agents/
├── pipeline.py               # main entry point
├── classifier_agent.py       # email classifier (Haiku)
├── orchestrator_agent.py     # decomposes + fans out to workflow agents
├── workflow_agent.py         # skill-based tool-use loop (Sonnet + MCP)
├── mcp_server.py             # FastMCP server — all support backend tools
├── email_stream.py           # reads data/emails.csv
├── email_sanitizer.py        # pattern-based injection strip
├── input_screener.py         # LLM-based injection detector (optional)
├── eval_agent.py             # LLM-as-judge evaluation
├── improve_agent.py          # eval-driven skill/KB improvement
├── logger.py                 # shared logging config
├── skills/
│   ├── billing/
│   ├── general/
│   ├── returns/
│   └── technical_support/
└── data/
    ├── emails.csv            # email dataset (subject, body, answer, type, queue, priority, language)
    └── knowledge_base.json   # support policy KB used by search_knowledge_base tool
```

## Skills

Each skill `.md` file has a YAML frontmatter block and a system prompt body:

```yaml
---
name: process_refund
types: [refund, return, billing_dispute]
tools: [lookup_customer, check_order_status, process_refund, send_reply]
---
You are a refund specialist...
```

The `tools` list controls which MCP tools the agent can access. Add new skills by dropping `.md` files into the appropriate `skills/<queue>/` directory.
