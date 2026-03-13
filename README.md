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

A customer support agent system built on Azure AI Foundry. Demonstrates Azure-native patterns: `AgentsClient` with persistent threads, `FileSearchTool` for managed RAG (no local embedding model), `ConnectedAgentTool` for multi-agent orchestration, Azure AI Content Safety guardrails on input and output, and OpenTelemetry traces + logs routed to Application Insights via `configure_azure_monitor()`.

**Platform:** Azure AI Foundry (GPT-4o, Azure File Search vector store, Azure AI Content Safety)

### [agent-openai](agent-openai/)

The same pipeline built directly on the OpenAI APIs, extended with Supervised Fine-Tuning (SFT) to bake agent behaviour guidelines into model weights. Demonstrates the full SFT lifecycle: dataset generation with quality filtering, fine-tuning job management, and automated evaluation comparing the fine-tuned model against the base model using the Responses API as judge.

**Transport:** in-process (OpenAI Assistants API with `file_search` + function tools for specialist agents)

**SFT:** `gpt-4o-mini-2024-07-18` fine-tuned to follow behaviour guidelines without needing them in the inference prompt — verified by `sft/evaluate.py` which scores both models side-by-side on 20 held-out examples.