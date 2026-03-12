# Customer Support Agent Pipeline — Azure AI Foundry

A multi-agent customer support pipeline built on Azure AI Foundry. Incoming emails are classified, routed to specialist agents, evaluated by an LLM judge, and iteratively improved by an automated improver — all running on Azure AI Agents with `DefaultAzureCredential`.

## Architecture

```
emails.csv
    │
    ▼
[Classifier]               ← gpt-4o-mini: queue, priority, type
    │
    ▼
[Orchestrator]             ← gpt-4o: decomposes email → specialist agent(s)
    │
    ├── [Specialist agent: technical_support]
    ├── [Specialist agent: billing]          ← parallel via ThreadPoolExecutor
    ├── [Specialist agent: returns]
    └── [Specialist agent: general]
            │
            ├── FunctionTool  (lookup_customer, get_ticket_history, create_ticket, ...)
            ├── FileSearchTool (Azure vector store — KB + guidelines)
            └── CodeInterpreterTool (billing only — refund/proration math)
    │
    ▼
[Merge]                    ← gpt-4o: merges multi-specialist replies into one
    │
    ▼
[Content Safety guardrail] ← Azure AI Content Safety: screens input + output
    │
    ▼
[Evaluator]                ← gpt-4o-mini LLM-as-judge: action / completeness / tone (1–5)
    │
    ▼
[Improver]                 ← gpt-4o: proposes skill_edit / kb_entry / agent_guideline
                              applies to skills/*.md + knowledge_base.json + agent_guidelines.json
                              re-uploads affected KB category to vector store
```

### Azure AI Foundry patterns demonstrated

| Pattern | Where |
|---|---|
| `AgentsClient` + `FunctionTool` (auto function dispatch) | `specialist_agents.py` |
| `FileSearchTool` + vector store (managed RAG) | `specialist_agents.py`, `kb_setup.py` |
| `CodeInterpreterTool` (managed sandbox) | `specialist_agents.py` (billing) |
| Multi-agent fan-out with `ThreadPoolExecutor` | `orchestrator_agent.py` |
| `DefaultAzureCredential` (az login / Managed Identity) | all modules |
| Azure AI Content Safety input/output guardrails | `guardrails.py` |
| OpenTelemetry tracing → console + Azure Monitor | `tracing.py` |
| Per-category vector store file management | `kb_setup.py` |

## Setup

**1. Install dependencies**

```bash
cd agent-azure
pip install -r requirements.txt
```

**2. Authenticate**

```bash
az login
```

**3. Configure environment**

Required variables in `.env`:

```
PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
MODEL_DEPLOYMENT_NAME=gpt-4o
FAST_MODEL=gpt-4o-mini

CONTENT_SAFETY_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
CONTENT_SAFETY_KEY=<key>

VECTOR_STORE_ID=          # set after running kb_setup.py
LOG_LEVEL=INFO            # set to DEBUG for step-level traces
```

Optional (enables Azure Monitor tracing + log ingestion):
```
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...
```

**4. Upload the knowledge base**

```bash
python kb_setup.py
# uploads per-category files: kb_billing.md, kb_returns.md, etc.
# prints VECTOR_STORE_ID=vs_xxxx on first run — paste into .env
```

**5. Run the pipeline**

```bash
python pipeline.py                        # 3 emails, eval + improve on
python pipeline.py --limit 10             # 10 emails
python pipeline.py --limit 5 --no-improve # eval only, no skill changes
python pipeline.py --no-eval --limit 3    # orchestration only
python pipeline.py --offset 10 --limit 5  # emails 11–15
```

## Key files

```
agent-azure/
├── pipeline.py              # main entry point — classify → orchestrate → eval → improve loop
├── classifier.py            # email → {queue, priority, type, agent_key}
├── orchestrator_agent.py    # decompose → fan-out → merge
├── specialist_agents.py     # create/run/cleanup specialist agents with FunctionTool + FileSearch
├── tools.py                 # ALL_TOOLS registry — Python functions dispatched by FunctionTool
├── skills.py                # loads skill .md files, selects skill by type/subject
├── evaluator.py             # LLM-as-judge scoring + eval_output.md writer
├── improver.py              # generates + applies improvement proposals
├── kb_setup.py              # uploads KB + guidelines to Azure vector store
├── guardrails.py            # Azure AI Content Safety screening
├── tracing.py               # OpenTelemetry setup (console + Azure Monitor)
├── logger.py                # shared logging — agents.* hierarchy, LOG_LEVEL env var
├── store.py                 # JSON-backed persistence (training set, guidelines, results)
├── requirements.txt
├── .env
└── data/
    ├── emails.csv                  # evaluation dataset
    ├── knowledge_base.json         # KB source (uploaded per-category to vector store)
    ├── agent_guidelines.json       # agent behaviour patterns (uploaded to vector store)
    ├── training_set.json           # regression emails per skill
    ├── pipeline_results.json       # all pipeline run results
    └── skills/
        ├── billing/
        │   ├── billing_inquiry.md
        │   └── process_refund.md   # includes CodeInterpreterTool
        ├── returns/
        │   └── initiate_return.md
        ├── technical_support/
        │   ├── diagnose_incident.md
        │   └── handle_request.md
        └── general/
            └── general_inquiry.md
```

## Skills

Each specialist agent is given a skill — a markdown file that defines its workflow, tools, and reply format. The skill is selected by matching the email's `type` (Incident, Request, etc.) against the skill's frontmatter:

```markdown
---
name: diagnose_incident
agent: technical_support
types: [Incident, Problem]
tools: [lookup_customer, get_ticket_history, create_ticket, escalate_to_human]
---
```

The `tools` list drives which Python functions are registered as `FunctionTool` and whether `CodeInterpreterTool` is added to the agent's toolset.

## Knowledge base

Entries are stored in `data/knowledge_base.json` and uploaded to an Azure managed vector store split by category. Azure handles chunking, embedding, and retrieval — no local embedding model required.

| Category | File in vector store |
|---|---|
| Billing | `kb_billing.md` |
| Returns | `kb_returns.md` |
| Technical | `kb_technical.md` |
| General | `kb_general.md` |
| Agent guidelines | `agent_guidelines.md` |

When the improver adds a new KB entry, only the affected category file is replaced.

## Improve loop

After each email run, if the eval score is below `--min-score` (default 4.5/5), the improver:

1. Sends the skill file + failing example to gpt-4o
2. Receives proposals: `skill_edit`, `kb_entry`, `agent_guideline`, or `new_skill`
3. Applies them to the local files and re-uploads only the changed KB category
4. Re-runs the skill against stored regression emails to check for regressions
5. Rolls back a `skill_edit` if regression threshold (3.5) is breached

## Logging and tracing

Set `LOG_LEVEL=DEBUG` in `.env` to see per-step traces:

```
DEBUG [agents.orchestrator_agent] decompose → agents=['technical_support'] reason=...
DEBUG [agents.specialist_agents]  step 1  fn: lookup_customer   args={"keyword": "login"}
DEBUG [agents.specialist_agents]  step 2  fn: get_ticket_history args={"customer_id": "CUST-001"}
DEBUG [agents.specialist_agents]  step 3  code_interpreter       output: Customer: CUST-001 ...
DEBUG [agents.specialist_agents]  step 4  file_search            files=['kb_technical.md']
```

Set `APPLICATIONINSIGHTS_CONNECTION_STRING` to route all traces and logs to Azure Monitor / Application Insights automatically via `configure_azure_monitor`.

## Demo scripts

Two standalone scripts demonstrate basic Azure AI Agents patterns independently of the pipeline:

- `kb_agent.py` — interactive KB Q&A via FileSearchTool
- `demo_orchestrator.py` — ConnectedAgentTool multi-agent demo
