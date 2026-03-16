# Customer Support Agent Pipeline — OpenAI + SFT

A multi-agent customer support pipeline built on the OpenAI APIs, extended with
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
sft/generate_dataset.py     ← text-only: ~160 train + 20 eval (40+5 per domain, quality-filtered)
    │   OR                     system = KB (domain-filtered) + all guidelines
    │                          assistant = ground-truth answer from emails.csv
sft/generate_tool_dataset.py← tool-call traces: ~20 train + 12 eval (5+3 per domain)
    │                          gpt-4o (teacher) runs Chat Completions tool-dispatch loop
    │                          captures full trace: tool_calls → results → final reply
    │                          filtered: examples with 0 tool calls discarded
    ▼
sft/fine_tune.py            ← uploads train.jsonl or train_tool.jsonl, starts SFT job
    │                          saves model ID to data/sft/model_id.txt
    ▼
sft/compare_pipeline.py     ← runs each email through the full pipeline twice
                               base model: classify → orchestrate (tools + file_search) → judge
                               ft model:   classify → orchestrate (tools + file_search) → judge
                               compares action/completeness/tone scores + tool call counts
                               writes compare_report.md after every email
```

**What fine-tuning proves:** If the fine-tuned model scores higher than the base model on
action/completeness/tone when running the full pipeline (with real tool calls and KB
retrieval), SFT improved domain adaptation. The comparison is honest because both models
use identical infrastructure — the only difference is model weights.

### OpenAI API patterns demonstrated

| Pattern | Where |
|---|---|
| Responses API with `file_search` + structured output | `sft/compare_pipeline.py` (judge) |
| Function tools with manual dispatch loop | `specialist_agents.py`, `agent_utils.py` |
| `file_search` + vector store (managed RAG) | `specialist_agents.py`, `kb_setup.py` |
| `response_format=json_object` (structured output) | `classifier.py`, `evaluator.py` |
| Chat Completions for stateless calls | `classifier.py`, `evaluator.py`, `improver.py` |
| Assistants API for stateful agent runs with tools | `specialist_agents.py` |
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

**Step 2a — Generate text-only training data** (domain adaptation)

```bash
python sft/generate_dataset.py
# writes data/sft/train.jsonl (~160 examples) + data/sft/eval.jsonl (20 examples)
# filters out short, JSON-formatted, or PII-placeholder answers automatically
# prints estimated training tokens and cost
```

**Step 2b — Generate tool-call training data** (teach tool use)

```bash
python sft/generate_tool_dataset.py
# gpt-4o (teacher) runs Chat Completions tool-dispatch loop on each email
# captures full trace: tool_calls → tool results → final reply
# writes data/sft/train_tool.jsonl (~20 examples) + eval_tool.jsonl (~12 examples)
# examples with zero tool calls are discarded automatically
```

**Step 3 — Fine-tune**

```bash
python sft/fine_tune.py
# uploads train.jsonl (or pass --train-file data/sft/train_tool.jsonl for tool-call SFT)
# starts gpt-4o-mini-2024-07-18 SFT job, polls to completion
# saves model ID to data/sft/model_id.txt
# add FINETUNED_MODEL=ft:gpt-4o-mini-... to .env
```

**Step 4 — Compare**

```bash
python sft/compare_pipeline.py
# runs 20 emails through the full pipeline with both models (real tool calls + file_search)
# prints per-email scores and tool usage, writes data/sft/compare_report.md after every email
# report includes actual replies and tools called for eyeballing quality
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
│   ├── generate_guidelines.py   # extract behavioural guidelines from emails.csv via LLM
│   ├── generate_dataset.py      # text-only dataset: train.jsonl + eval.jsonl
│   ├── generate_tool_dataset.py # tool-call dataset: train_tool.jsonl + eval_tool.jsonl (gpt-4o teacher)
│   ├── fine_tune.py             # upload data, start SFT job, poll to completion
│   └── compare_pipeline.py      # compare base vs. fine-tuned using the full pipeline (real tool calls)
└── data/
    ├── emails.csv                  # evaluation dataset (16,338 English emails)
    ├── knowledge_base.json         # KB source (uploaded per-category to vector store)
    ├── agent_guidelines.json       # agent behaviour patterns (expanded by generate_guidelines.py)
    ├── training_set.json           # regression emails per skill
    ├── pipeline_results.json       # all pipeline run results
    ├── sft/
    │   ├── train.jsonl             # text-only training examples (generate_dataset.py)
    │   ├── eval.jsonl              # text-only held-out examples
    │   ├── train_tool.jsonl        # tool-call training examples (generate_tool_dataset.py)
    │   ├── eval_tool.jsonl         # tool-call held-out examples
    │   ├── model_id.txt            # fine-tuned model ID (written by fine_tune.py)
    │   └── compare_report.md       # base vs. fine-tuned pipeline comparison report
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

**What SFT provides — domain adaptation, not guideline baking:**

SFT on customer support examples teaches the model how support emails are structured, what
good replies look like, appropriate tone and professional register, the right level of detail
per domain, and how to incorporate retrieved KB content effectively. These are genuine
improvements measurable by the LLM judge.

**Why guidelines stay in the prompt:**

The 27+ agent guidelines are specific, conditional operational decision rules — e.g. *"when
customer mentions CI/CD, ask for project environment and tooling before routing"* or *"offer
to schedule a call for complex troubleshooting involving critical access"*. SFT teaches
probabilistic tendencies; it cannot guarantee a specific rule fires at the right moment.
Guidelines also grow over time via the improver loop — retraining every time a guideline is
added is impractical. Guidelines belong in the prompt where they can be updated instantly
without touching model weights.

`evaluate.py` tests this correctly: both models use the same system prompt (base instructions
+ guidelines + file_search). Any quality gap reflects domain adaptation, not missing context.

**Training data format:**

Each example includes:
- System: base instructions + domain-filtered KB entries (simulates retrieved context) + guidelines
- User: email subject + body
- Assistant: ground-truth answer from the dataset

The KB in the system prompt during training mirrors what `file_search` retrieves at inference,
so the model learns to use retrieved context consistently.

Training examples are filtered automatically: answers shorter than 80 characters, JSON-formatted
responses, and answers containing raw PII placeholders (e.g. `<name>`, `<tel_num>`) are excluded
to prevent noise from polluting the training signal.

**Cost estimate (~160 training examples on gpt-4o-mini-2024-07-18):**
~640K tokens × 3 epochs × $0.003/1K = ~$5.76 per run.
Pricing: training $0.003/1K tokens · inference input $0.0003/1K · inference output $0.0012/1K.
Costs scale with epoch count and total context length (KB + guidelines in each example can be long).
Check the OpenAI dashboard after each job for the exact amount.

## Logging and tracing

Set `OTLP_ENDPOINT` to route traces to a collector (Jaeger, Grafana Tempo, etc.).
Without it, set `TRACING=true` in `.env` to emit spans to the console, or `TRACING=false`
(the default) to suppress console output while still recording spans internally.

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
