# Customer Support Agent Pipeline — Azure AI Foundry

A multi-agent customer support pipeline built on Azure AI Foundry. Incoming emails are classified, decomposed into specialist agents, evaluated by managed Azure AI Evaluation SDK evaluators, and iteratively improved by an automated improver — all running on Azure AI Agents with `DefaultAzureCredential`.

## Architecture

```
emails.csv
    │
    ▼
[Classifier]               ← gpt-4o-mini: queue, priority, type
    │                         response_format=json_object (guaranteed JSON)
    ▼
[Decomposer]               ← gpt-4o-mini: selects which specialist(s) to call
    │
    ▼
[Specialist agent(s)]      ← gpt-4o, sequential, one per concern
    ├── FunctionTool        lookup_customer, get_ticket_history, create_ticket,
    │                       check_order_status, process_refund, escalate_to_human
    └── FileSearchTool      Azure vector store — KB + guidelines
    │
    ▼
[Merge]                    ← gpt-4o: merges multi-specialist replies (single-specialist: skipped)
    │
    ▼
[Content Safety guardrail] ← Azure AI Content Safety: screens input + output
    │
    ▼
[Evaluator]                ← Azure AI Evaluation SDK: groundedness / relevance / coherence / fluency (1–5)
    │                         CoherenceEvaluator, FluencyEvaluator, RelevanceEvaluator, GroundednessEvaluator
    ▼
[Improver]                 ← gpt-4o: proposes skill_edit / kb_entry / agent_guideline
                              applies to skills/*.md + knowledge_base.json + agent_guidelines.json
                              re-uploads affected KB category to vector store
```

### Azure AI Foundry patterns demonstrated

| Pattern | Where |
|---|---|
| `FunctionTool` with auto function dispatch | `specialist_agents.py`, `tools.py` |
| `FileSearchTool` + vector store (managed RAG) | `specialist_agents.py`, `kb_setup.py` |
| `AgentsResponseFormat(type="json_object")` (structured output) | `classifier.py` |
| Azure AI Evaluation SDK managed evaluators | `evaluator.py` |
| Agent reuse across calls (pool pattern) | `pipeline.py` — `_AgentPool` |
| `DefaultAzureCredential` (az login / Managed Identity) | all modules |
| Azure AI Content Safety input/output guardrails | `guardrails.py` |
| OpenTelemetry tracing → console + Azure Monitor | `tracing.py` |
| Per-category vector store file management | `kb_setup.py` |
| `ConnectedAgentTool` multi-agent demo | `demo_orchestrator.py` |

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

VECTOR_STORE_ID=          # set after running kb_setup.py
LOG_LEVEL=INFO            # DEBUG for verbose agent output
```

Content Safety uses `DefaultAzureCredential` — no key needed. Assign the
`Cognitive Services User` role to your identity (or Managed Identity in production):

```bash
az role assignment create \
  --role "Cognitive Services User" \
  --assignee $(az ad signed-in-user show --query id -o tsv) \
  --scope $(az cognitiveservices account show \
      --name <content-safety-resource-name> \
      --resource-group <rg> --query id -o tsv)
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
├── orchestrator_agent.py    # decompose → fan-out (sequential) → merge
├── specialist_agents.py     # Foundry specialist agent factory: FunctionTool + FileSearch + skills
├── tools.py                 # ALL_TOOLS registry — Python functions dispatched by FunctionTool
├── skills.py                # loads/versions skill .md files; selects skill per email type
├── evaluator.py             # Azure AI Evaluation SDK scoring + eval_output.md writer
├── improver.py              # generates + applies improvement proposals
├── kb_setup.py              # uploads KB + guidelines to Azure vector store
├── guardrails.py            # Azure AI Content Safety screening
├── tracing.py               # OpenTelemetry setup (console + Azure Monitor)
├── agent_utils.py           # run_with_retry helper
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
        │   └── process_refund.md
        ├── returns/
        │   └── initiate_return.md
        ├── technical_support/
        │   ├── diagnose_incident.md
        │   └── handle_request.md
        └── general/
            └── general_inquiry.md
```

## Orchestration

The pipeline uses a three-step decompose → fan-out → merge flow:

**Decompose:** A lightweight `gpt-4o-mini` agent reads the email and returns which specialist(s) are needed (`technical_support`, `billing`, `returns`, or `general`). Most emails need only one.

**Fan-out:** Each specialist is a dedicated Foundry agent with:
- A **skill file** as its system prompt (`data/skills/{domain}/{skill}.md`) — selected by matching the email's `type` against the skill's frontmatter
- **`FunctionTool`** for CRM and ticketing actions (`lookup_customer`, `create_ticket`, `escalate_to_human`, etc.) — dispatched in-process via `enable_auto_function_calls`
- **`FileSearchTool`** for KB and guideline retrieval from the Azure vector store

`enable_auto_function_calls` is registered once on the shared client at startup with all tools. Each agent's `FunctionTool` definition controls which tools the model actually calls. Specialists run sequentially.

**Merge:** If multiple specialists ran, a `gpt-4o` merge agent combines their replies into one coherent customer response. For single-specialist emails this step is skipped.

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

## Agent pooling

The classifier and improver agents are created once at pipeline startup and reused across all emails. Threads are still per-call (they hold conversation state). Agents are deleted once on pipeline exit:

```
startup  → create classifier, improver, kb_merger, guideline_merger
email 1  → classify (reuse classifier) → orchestrate → eval (Azure AI Evaluation SDK)
email 2  → classify (reuse classifier) → orchestrate → eval (Azure AI Evaluation SDK)
...
exit     → delete all 4 pooled agents
```

Evaluation no longer requires a Foundry agent — the Azure AI Evaluation SDK calls the model directly as a stateless API call, so there is no agent lifecycle to manage for eval.

## Improve loop

After each email run, if the eval score is below `--min-score` (default 4.5/5), the improver:

1. Sends the skill file + failing example to gpt-4o
2. Receives proposals: `skill_edit`, `kb_entry`, `agent_guideline`, or `new_skill`
3. Applies them to the local files and re-uploads only the changed KB category
4. Re-runs the skill against stored regression emails to check for regressions
5. Rolls back a `skill_edit` if regression threshold (3.5) is breached

## Logging and tracing

Set `APPLICATIONINSIGHTS_CONNECTION_STRING` to route all traces and logs to Azure Monitor / Application Insights via `configure_azure_monitor`.

OpenTelemetry span hierarchy:
```
pipeline.email
├── pipeline.classify              attrs: queue, priority, type
├── pipeline.orchestrate           attrs: email.subject, classification.queue
│   ├── pipeline.decompose         attrs: agents_selected
│   ├── pipeline.specialist.{key}  attrs: skill_name, tools_called, files_searched
│   └── pipeline.merge             attrs: specialist_count
└── eval                           attrs: avg, groundedness, relevance, coherence, fluency
```

Set `LOG_LEVEL=DEBUG` to see per-step function/file_search/code_interpreter traces for each specialist run.

## Demo scripts

Two standalone scripts demonstrate Azure AI Agents patterns independently of the pipeline:

- `kb_agent.py` — interactive KB Q&A via `FileSearchTool`
- `demo_orchestrator.py` — `ConnectedAgentTool` multi-agent demo (kb-specialist + triage-specialist)
