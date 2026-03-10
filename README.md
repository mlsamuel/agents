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