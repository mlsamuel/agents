# agents

A monorepo of customer support agent pipelines built with Claude. Each subproject processes emails through the same classify → orchestrate → eval → improve loop, but demonstrates a different tool transport pattern.

## Projects

### [agent-mcp](agent-mcp/)

Tools are served over the Model Context Protocol (MCP) via a persistent FastMCP HTTP server. Workflow agents connect to it using `streamablehttp_client` and discover tools at runtime through the protocol.

**Transport:** HTTP (MCP over streamable-HTTP, port 8765)

### [agent-cli](agent-cli/)

Tools are exposed as a Click CLI (`cli.py`) that outputs structured JSON. Workflow agents invoke it as a subprocess — no server process, no sockets. Inspired by the [googleworkspace/cli](https://github.com/googleworkspace/cli) pattern.

**Transport:** subprocess (`python cli.py <namespace> <command> [--flags]`)

## Shared infrastructure

Both projects use the same Postgres + pgvector backend (skills, knowledge base, agent guidelines, training set), the same email dataset, and the same eval/improve loop. The `.env` file at the repo root is shared.

```
agents/
├── agent-mcp/    # MCP transport variant
├── agent-cli/    # CLI transport variant
└── .env          # shared environment variables
```

## Setup

Copy `.env.example` from either subproject and fill in your credentials:

```bash
cp agent-cli/.env.example .env
# ANTHROPIC_API_KEY=...
# DATABASE_URL=postgresql://user:pass@host/dbname
```

Then follow the setup instructions in the subproject README.
