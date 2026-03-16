# agents

A monorepo of customer support agent pipelines built with Claude. Each subproject processes emails through the same classify → orchestrate → eval → improve loop, but demonstrates a different tool transport pattern.

## Projects

### [agent-mcp](agent-mcp/)

Tools are served over the Model Context Protocol (MCP) via a persistent FastMCP HTTP server. Workflow agents connect to it using `streamablehttp_client` and discover tools at runtime through the protocol.

**Transport:** HTTP (MCP over streamable-HTTP, port 8765)

### [agent-cli](agent-cli/)

Tools are exposed as a Click CLI (`cli.py`) that outputs structured JSON. Workflow agents invoke it as a subprocess — no server process, no sockets. Inspired by the [googleworkspace/cli](https://github.com/googleworkspace/cli) pattern.

**Transport:** subprocess (`python cli.py <namespace> <command> [--flags]`)

Eval results are persisted to Postgres and viewable as a [static showcase](https://htmlpreview.github.io/?https://github.com/mlsamuel/agents/blob/main/agent-cli/ui/showcase/index.html) — no servers required.

### [agent-langgraph](agent-langgraph/)

The same pipeline orchestrated as an explicit LangGraph `StateGraph`. Demonstrates LangGraph-specific patterns: Send API for parallel fan-out, compiled sub-graphs as nodes, `ToolNode` for in-process tool execution, a critic reflection loop inside each specialist agent, `interrupt()` for human-in-the-loop escalation review, and `AsyncPostgresSaver` for checkpoint persistence.

**Transport:** in-process (`ToolNode` + `@tool` decorated functions — no server, no subprocess)

Eval results and the escalation review UI are viewable as a [static showcase](https://htmlpreview.github.io/?https://github.com/mlsamuel/agents/blob/main/agent-langgraph/ui/showcase/index.html) — no servers required.

### [agent-azure](agent-azure/)

A customer support agent system built on Azure AI Foundry. Demonstrates Azure-native patterns: `AgentsClient` with persistent threads, `FileSearchTool` for managed RAG (no local embedding model), Azure AI Evaluation SDK managed evaluators (groundedness, relevance, coherence, fluency), a pre-send validation gate (`GroundednessEvaluator` against retrieved KB content) that blocks ungrounded replies before they reach the customer, Azure AI Content Safety guardrails on input and output, and OpenTelemetry traces + logs routed to Application Insights via `configure_azure_monitor()`.

**Platform:** Azure AI Foundry (GPT-4o, Azure File Search vector store, Azure AI Content Safety)

### [agent-openai](agent-openai/)

The same pipeline built directly on the OpenAI APIs, extended with Supervised Fine-Tuning (SFT) for domain adaptation. Demonstrates the full SFT lifecycle: tool-call trace generation via teacher-student distillation (gpt-4o → gpt-4.1-mini), fine-tuning job management, and pipeline comparison running both models through real tool calls and file_search side-by-side.

**Transport:** in-process (OpenAI Assistants API with `file_search` + function tools for specialist agents)

**SFT:** `gpt-4.1-mini-2025-04-14` fine-tuned on tool-call traces (gpt-4o teacher → student distillation) for domain adaptation — compared against the base model by `sft/compare_pipeline.py` which runs both models through the full pipeline with real tool calls and scores them side-by-side.