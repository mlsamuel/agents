# Customer Support Agent Pipeline — OpenAI Assistants + SFT

A multi-agent customer support pipeline built on the OpenAI Assistants API, extended with
Supervised Fine-Tuning (SFT) to bake agent behaviour guidelines into model weights.

## Architecture

```
emails.csv
    │
    ▼
[Classifier]               ← gpt-4o-mini: queue, priority, type
    │                         Chat Completions + response_format=json_object
    ▼
[Decomposer]               ← gpt-4o-mini: selects which specialist(s) to call
    │                         Chat Completions + response_format=json_object
    ▼
[Specialist agent(s)]      ← gpt-4o (or fine-tuned), sequential, one per concern
    ├── Function tools       lookup_customer, get_ticket_history, create_ticket,
    │                        check_order_status, process_refund, escalate_to_human
    └── file_search          OpenAI vector store — KB + guidelines
    │
    ▼
[Merge]                    ← gpt-4o: merges multi-specialist replies (single: skipped)
    │
    ▼
[Moderation guardrail]     ← openai.moderations: screens input + output
    │
    ▼
[Evaluator]                ← gpt-4o-mini LLM-as-judge: action / completeness / tone (1–5)
    │                         Chat Completions + response_format=json_object
    ▼
[Improver]                 ← gpt-4o: proposes skill_edit / kb_entry / agent_guideline
                              applies to skills/*.md + knowledge_base.json + agent_guidelines.json
                              re-uploads affected KB category to vector store
```

### SFT pipeline

```
emails.csv (en only)
    │
    ▼
sft/generate_guidelines.py  ← GPT-4o extracts behavioural guidelines from email examples
    │                          merges into data/agent_guidelines.json
    ▼
sft/generate_dataset.py     ← stratified sample: 100 train + 20 eval (25+5 per domain)
    │                          system = KB (domain-filtered) + all guidelines
    │                          assistant = ground-truth answer from emails.csv
    ▼
sft/fine_tune.py            ← uploads train.jsonl, starts gpt-4o-mini SFT job, polls to completion
    │                          saves model ID to data/sft/model_id.txt
    ▼
sft/evaluate.py             ← both models use Assistants + file_search for KB retrieval
                               base: file_search + guidelines in system prompt
                               fine-tuned: file_search only (no guidelines in prompt)
                               LLM judge scores both; prints comparison table
```

**What fine-tuning proves:** If the fine-tuned model matches the base model's scores
without needing the guidelines in its system prompt, the training successfully baked in
the behavioural patterns. KB retrieval still happens at runtime via `file_search`.

### OpenAI API patterns demonstrated

| Pattern | Where |
|---|---|
| Function tools with manual dispatch loop | `specialist_agents.py`, `agent_utils.py` |
| `file_search` + vector store (managed RAG) | `specialist_agents.py`, `kb_setup.py` |
| `response_format=json_object` (structured output) | `classifier.py`, `evaluator.py` |
| Chat Completions for stateless calls | `classifier.py`, `evaluator.py`, `improver.py` |
| Assistants API for stateful agent runs | `specialist_agents.py`, `sft/evaluate.py` |
| `openai.moderations` input/output guardrails | `guardrails.py` |
| Supervised Fine-Tuning (SFT) | `sft/` |
| OpenTelemetry tracing → console / OTLP | `tracing.py` |
| Per-category vector store file management | `kb_setup.py` |

## Setup

**1. Install dependencies**

```bash
cd agent-openai
pip install -r requirements.txt
```

**2. Configure environment**

```bash
cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY
```

**3. Upload the knowledge base**

```bash
python kb_setup.py
# prints VECTOR_STORE_ID=vs_xxxx on first run — paste into .env
```

**4. Run the pipeline**

```bash
python pipeline.py                        # 3 emails, eval + improve on
python pipeline.py --limit 10             # 10 emails
python pipeline.py --limit 5 --no-improve # eval only, no skill changes
python pipeline.py --no-eval --limit 3    # orchestration only
python pipeline.py --offset 10 --limit 5  # emails 11–15
```

## SFT workflow

**Step 1 — Extract more guidelines**

```bash
python sft/generate_guidelines.py
# samples 40 emails per domain, extracts ~6 new guidelines each
# merges into data/agent_guidelines.json
```

**Step 2 — Generate training data**

```bash
python sft/generate_dataset.py
# writes data/sft/train.jsonl (100 examples) + data/sft/eval.jsonl (20 examples)
# prints estimated training tokens and cost
```

**Step 3 — Fine-tune**

```bash
python sft/fine_tune.py
# uploads train.jsonl, starts gpt-4o-mini-2024-07-18 SFT job
# polls to completion, saves model ID to data/sft/model_id.txt
# add FINETUNED_MODEL=ft:gpt-4o-mini-... to .env
```

**Step 4 — Evaluate**

```bash
python sft/evaluate.py
# runs 20 held-out examples through both models using Assistants + file_search
# prints comparison table, saves data/sft/eval_report.md
```

## Key files

```
agent-openai/
├── pipeline.py              # main entry point — classify → orchestrate → eval → improve
├── classifier.py            # email → {queue, priority, type} — Chat Completions
├── orchestrator_agent.py    # decompose → fan-out (sequential) → merge
├── specialist_agents.py     # OpenAI Assistants: function tools + file_search + skill
├── tools.py                 # ALL_TOOLS registry + TOOL_DEFINITIONS (OpenAI schemas)
├── skills.py                # loads/versions skill .md files
├── evaluator.py             # LLM-as-judge scoring + eval_output.md writer
├── improver.py              # generates + applies improvement proposals
├── kb_setup.py              # uploads KB + guidelines to OpenAI vector store
├── guardrails.py            # OpenAI Moderation API screening
├── agent_utils.py           # run_with_tool_dispatch + run_simple helpers
├── tracing.py               # OpenTelemetry setup (console + OTLP)
├── store.py                 # JSON-backed persistence (training set, guidelines, results)
├── logger.py                # shared logging — agents.* hierarchy, LOG_LEVEL env var
├── requirements.txt
├── .env.example
├── sft/
│   ├── generate_guidelines.py  # extract behavioural guidelines from emails.csv via LLM
│   ├── generate_dataset.py     # build train.jsonl + eval.jsonl for fine-tuning
│   ├── fine_tune.py            # upload data, start SFT job, poll to completion
│   └── evaluate.py             # compare base vs. fine-tuned on held-out eval set
└── data/
    ├── emails.csv                  # evaluation dataset (16,338 English emails)
    ├── knowledge_base.json         # KB source (uploaded per-category to vector store)
    ├── agent_guidelines.json       # agent behaviour patterns (expanded by generate_guidelines.py)
    ├── training_set.json           # regression emails per skill
    ├── pipeline_results.json       # all pipeline run results
    ├── sft/
    │   ├── train.jsonl             # 100 SFT training examples
    │   ├── eval.jsonl              # 20 held-out eval examples
    │   ├── model_id.txt            # fine-tuned model ID (written by fine_tune.py)
    │   └── eval_report.md          # base vs. fine-tuned comparison report
    └── skills/
        ├── billing/
        ├── returns/
        ├── technical_support/
        └── general/
```

## Orchestration

The pipeline uses a three-step decompose → fan-out → merge flow:

**Decompose:** `gpt-4o-mini` reads the email and returns which specialist(s) are needed
(`technical_support`, `billing`, `returns`, or `general`). Most emails need only one.

**Fan-out:** Each specialist is an OpenAI Assistant with:
- A **skill file** as its `instructions` — selected by matching the email's `type`
- **Function tools** for CRM and ticketing actions — dispatched in-process by `run_with_tool_dispatch`
- **`file_search`** for KB and guideline retrieval from the OpenAI vector store

Specialists run sequentially. The tool dispatch loop in `agent_utils.py` handles `requires_action`
states (function calls) before returning the completed run.

**Merge:** If multiple specialists ran, `gpt-4o` combines their replies.

## Knowledge base

KB is stored in `data/knowledge_base.json` and uploaded to an OpenAI vector store split by category.
OpenAI handles chunking, embedding, and retrieval — no local embedding model required.

| Category | File in vector store |
|---|---|
| Billing | `kb_billing.md` |
| Returns | `kb_returns.md` |
| Technical | `kb_technical.md` |
| General | `kb_general.md` |
| Agent guidelines | `agent_guidelines.md` |

When the improver adds a new KB entry, only the affected category file is replaced.

## SFT design rationale

**Why guidelines go into training, not the prompt:**

In production, the KB grows continuously — too large to embed in every system prompt.
The solution is `file_search` for all KB content at inference time. However, behavioural
guidelines (asking clarifying questions, escalation rules, tone patterns) are stable and
policy-driven. They make natural SFT training signal: bake them into the model once, then
remove them from the inference prompt. The evaluate.py script proves this works by measuring
whether the fine-tuned model maintains quality without the guidelines.

**Training data format:**

Each example includes:
- System: base instructions + domain-filtered KB entries (simulates retrieved context) + guidelines
- User: email subject + body
- Assistant: ground-truth answer from the dataset

The KB in the system prompt during training mirrors what file_search retrieves at inference,
so the model learns to use retrieved context consistently.

**Cost estimate (100 training examples on gpt-4o-mini-2024-07-18):**
~500K characters → ~125K tokens → ~$0.50 at $0.004/1K training tokens.
Varies with guideline count and KB entries included.

## Logging and tracing

Set `OTLP_ENDPOINT` to route traces to a collector (Jaeger, Grafana Tempo, etc.).
Without it, traces are emitted to the console via BatchSpanProcessor.

OpenTelemetry span hierarchy:
```
pipeline.email
├── pipeline.classify              attrs: queue, priority, type
├── pipeline.orchestrate           attrs: email.subject, classification.queue
│   ├── pipeline.decompose         attrs: agents_selected
│   ├── pipeline.specialist.{key}  attrs: skill_name, tools_called, files_searched
│   └── pipeline.merge             attrs: specialist_count
└── eval                           attrs: avg, action, completeness, tone
```

Set `LOG_LEVEL=DEBUG` to see per-step function/file_search traces for each specialist run.
