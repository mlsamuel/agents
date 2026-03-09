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

Both projects use the same Postgres + pgvector setup and the same email dataset. Each project manages its own database and has its own `.env` file — they are fully independent.

`agent-cli` additionally persists every eval run to `pipeline_runs` + `pipeline_results` tables and ships a static showcase at `agent-cli/ui/showcase/index.html` — open it in any browser with no servers required.

```
agents/
├── agent-mcp/    # MCP transport variant
│   └── .env      # per-project credentials + DATABASE_URL
└── agent-cli/    # CLI transport variant
    └── .env      # per-project credentials + DATABASE_URL
```

## Setup

Copy `.env.example` inside the subproject you want to run:

```bash
cp agent-cli/.env.example agent-cli/.env
# or
cp agent-mcp/.env.example agent-mcp/.env
# edit: ANTHROPIC_API_KEY=... DATABASE_URL=...
```

Then follow the setup instructions in the subproject README.
