# Customer Support Agent System — Azure AI Foundry Edition

A customer support agent system built on Azure AI Foundry. Queries are routed through a multi-agent orchestration layer to either a knowledge base specialist (RAG via Azure File Search) or a triage specialist for escalation. All interactions are traced with OpenTelemetry, logged as structured JSON, and screened by Azure AI Content Safety guardrails on both input and output.

## Architecture

```
User query
    │
    ▼
[Content Safety guardrail]     ← Azure AI Content Safety: screens input
    │
    ▼
[Orchestrator agent]           ← GPT-4o: routes to the right specialist
    │
    ├── [KB specialist]        ← GPT-4o + Azure File Search (vector store)
    │       │                     answers from knowledge_base.json
    │       │                     returns UNRESOLVED: if not in KB
    │       ▼
    └── [Triage specialist]    ← GPT-4o (conditional — only if UNRESOLVED)
            │                     classifies urgency: low / medium / high
            │                     returns ESCALATION_LEVEL: <level>
    │
    ▼
[Content Safety guardrail]     ← Azure AI Content Safety: screens output
    │
    ▼
Structured JSON log + OTel span
    │
    ├── Console (always)
    └── Azure Monitor / Application Insights (if configured)
```

### Azure AI Foundry patterns demonstrated

| Pattern | Where |
|---|---|
| `AgentsClient` + persistent threads | `kb_agent.py`, `orchestrator_agent.py` |
| `FileSearchTool` + vector store (managed RAG) | `kb_agent.py`, `orchestrator_agent.py` |
| `ConnectedAgentTool` multi-agent orchestration | `orchestrator_agent.py` |
| `DefaultAzureCredential` (az login) | all agents |
| Azure AI Content Safety input/output guardrails | `guardrails.py` |
| OpenTelemetry tracing → console + Azure Monitor | `tracing.py` |
| Structured JSON logging (per-turn latency, routing) | `kb_agent.py`, `orchestrator_agent.py` |

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

```bash
cp .env .env.local   # or edit .env directly
```

Required variables:

```
PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
MODEL_DEPLOYMENT_NAME=gpt-4o

CONTENT_SAFETY_ENDPOINT=https://<resource>.cognitiveservices.azure.com/
CONTENT_SAFETY_KEY=<key>

VECTOR_STORE_ID=          # set after running kb_setup.py
```

Optional (enables Azure Monitor tracing):
```
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...
```

**4. Upload the knowledge base**

Run once to create an Azure vector store from `data/knowledge_base.json`:

```bash
python kb_setup.py
# → prints VECTOR_STORE_ID=vs_xxxx
# paste it into .env
```

**5. Run**

```bash
# Single KB agent — direct Q&A from knowledge base
python kb_agent.py

# Multi-agent orchestrator — routes between KB and triage specialists
python orchestrator_agent.py
```

## Agents

### `kb_agent.py` — Knowledge Base Q&A

Interactive REPL that answers customer support questions from the knowledge base. Uses Azure File Search for retrieval — no local embedding model or database required.

- Multi-turn conversation via persistent Azure thread
- Input and output screened by Azure AI Content Safety
- OpenTelemetry span per turn with latency and run status
- Structured JSON log per turn

### `orchestrator_agent.py` — Multi-agent Orchestrator

Two-level agent system using `ConnectedAgentTool`:

1. **Orchestrator** — routes every query to `kb_agent` first
2. **KB specialist** — searches the vector store; returns `UNRESOLVED:` if not found
3. **Triage specialist** — classifies unresolved issues by urgency (`low` / `medium` / `high`)

Each response is tagged `[KB]` or `[ESCALATED]` in the terminal and as a span attribute (`routing.escalated`) in Application Insights.

### `agent.py` — Hello World

Minimal example: creates an agent with `CodeInterpreterTool`, runs a single turn, and cleans up. Starting point for understanding the Azure AI Agents SDK.

## Tracing

Spans are exported to the console by default. Set `APPLICATIONINSIGHTS_CONNECTION_STRING` to also send to Azure Monitor.

View in the portal: **Application Insights** → **Investigate** → **Transaction search**

Span hierarchy:
```
agent-session
    └── turn (× N)
            ├── run.status
            ├── run.latency_ms
            ├── guardrail.blocked (if applicable)
            └── routing.escalated (orchestrator only)
```

## Project structure

```
agent-azure/
├── agent.py                  # minimal hello-world agent (CodeInterpreterTool)
├── kb_agent.py               # KB Q&A agent (FileSearchTool + guardrails + tracing)
├── orchestrator_agent.py     # multi-agent orchestrator (ConnectedAgentTool)
├── guardrails.py             # Azure AI Content Safety input/output screening
├── tracing.py                # OpenTelemetry setup (console + Azure Monitor)
├── kb_setup.py               # one-time: uploads knowledge_base.json → Azure vector store
├── requirements.txt
├── .env
└── data/
    └── knowledge_base.json   # 28 Q&A entries: billing, returns, technical, general
```

## Knowledge base

28 entries across four categories, seeded from `data/knowledge_base.json` and uploaded to an Azure managed vector store. Azure handles chunking, embedding, and retrieval — no local embedding model or Postgres required.

| Category | Entries | Topics |
|---|---|---|
| Billing | 10 | Payment dates, methods, late fees, invoices, extra charges |
| General | 9 | Support hours, account tiers, software compatibility, smart home |
| Returns | 4 | Return window (30 days), process, refund timeline, exclusions |
| Technical | 4 | Password reset, outage reporting, browser support, maintenance |
