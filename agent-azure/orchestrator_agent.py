"""Multi-agent orchestration using Azure AI Foundry ConnectedAgentTool.

Architecture:
    User query
        ↓
    [Orchestrator] — routes to the right specialist
        ├── [KB Agent]      — answers from knowledge base via File Search
        └── [Triage Agent]  — classifies and escalates unresolved issues

Run:
    python orchestrator_agent.py
"""
import json
import logging
import os
import time
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import ConnectedAgentTool, FileSearchTool
from azure.identity import DefaultAzureCredential

from guardrails import GuardrailError, screen
from tracing import setup_tracing

load_dotenv()

# --- Structured JSON logging ---
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            payload.update(record.extra)
        return json.dumps(payload)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("orchestrator")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False
    return log


KB_AGENT_INSTRUCTIONS = """You are a customer support knowledge base lookup tool.
Search the knowledge base and return the answer verbatim from it.
Always cite the topic name you found the answer in.

If the knowledge base does not contain a relevant answer, you MUST respond with EXACTLY this format and nothing else:
UNRESOLVED: <one sentence describing the issue>

Do NOT attempt to answer from general knowledge. Do NOT add escalation language. Do NOT suggest next steps.
If you cannot find it in the knowledge base, output only the UNRESOLVED line."""

TRIAGE_AGENT_INSTRUCTIONS = """You are a customer support triage specialist.
You receive issues that could not be resolved from the knowledge base.
Classify the issue by urgency (low/medium/high) and recommend the next action:
- Low: suggest self-service resources
- Medium: schedule a callback within 24 hours
- High: escalate to a human agent immediately
Always end with: ESCALATION_LEVEL: <low|medium|high>"""

ORCHESTRATOR_INSTRUCTIONS = """You are a router with zero domain knowledge. You do not know anything about customer support, billing, returns, or technical issues. You cannot answer any question on your own.

Your only job is to call the right tool and return its output verbatim.

RULE 1: For every user message, call kb_agent immediately. Do not think about the answer first.
RULE 2: If kb_agent's response starts with "UNRESOLVED:", call triage_agent immediately with the original user question.
RULE 3: Return the tool's output word for word. Do not rephrase, summarize, or add anything.

You are forbidden from generating any response that does not come directly from a tool call."""


def get_response_text(messages) -> str:
    for msg in messages:
        if msg.role == "assistant":
            for part in msg.content:
                if hasattr(part, "text"):
                    return part.text.value
    return ""


def main() -> None:
    log = configure_logging()
    tracer = setup_tracing()

    vector_store_id = os.environ.get("VECTOR_STORE_ID", "")
    if not vector_store_id:
        print("ERROR: VECTOR_STORE_ID not set. Run kb_setup.py first.")
        return

    client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
    model = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o")

    with tracer.start_as_current_span("orchestrator-session") as session_span:
        # --- Create specialist agents ---
        file_search = FileSearchTool(vector_store_ids=[vector_store_id])
        kb_agent = client.create_agent(
            model=model,
            name="kb-specialist",
            instructions=KB_AGENT_INSTRUCTIONS,
            tools=file_search.definitions,
            tool_resources=file_search.resources,
        )
        log.info("KB agent created", extra={"extra": {"agent_id": kb_agent.id}})

        triage_agent = client.create_agent(
            model=model,
            name="triage-specialist",
            instructions=TRIAGE_AGENT_INSTRUCTIONS,
        )
        log.info("Triage agent created", extra={"extra": {"agent_id": triage_agent.id}})

        # --- Wrap specialists as ConnectedAgentTools ---
        kb_tool = ConnectedAgentTool(
            id=kb_agent.id,
            name="kb_agent",
            description="Searches the customer support knowledge base to answer questions about billing, returns, technical support, and general inquiries.",
        )
        triage_tool = ConnectedAgentTool(
            id=triage_agent.id,
            name="triage_agent",
            description="Classifies and escalates unresolved customer issues by urgency level.",
        )

        # --- Create orchestrator ---
        orchestrator = client.create_agent(
            model=model,
            name="support-orchestrator",
            instructions=ORCHESTRATOR_INSTRUCTIONS,
            tools=[*kb_tool.definitions, *triage_tool.definitions],
        )
        session_span.set_attribute("orchestrator.id", orchestrator.id)
        log.info("Orchestrator created", extra={"extra": {"agent_id": orchestrator.id}})

        thread = client.threads.create()
        session_span.set_attribute("thread.id", thread.id)

        print("\nCustomer Support Orchestrator")
        print("=" * 40)
        print("Agents: orchestrator → kb-specialist, triage-specialist")
        print("Type 'quit' to exit.\n")

        turn = 0
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input or user_input.lower() in {"quit", "exit", "q"}:
                break

            turn += 1
            with tracer.start_as_current_span("turn") as turn_span:
                turn_span.set_attribute("turn", turn)

                # Input guardrail
                try:
                    screen(user_input, label="input")
                except GuardrailError as e:
                    turn_span.set_attribute("guardrail.blocked", True)
                    log.warning("Input blocked", extra={"extra": {"turn": turn, "categories": e.categories}})
                    print("Agent: I'm sorry, I can't process that request.\n")
                    continue

                client.messages.create(thread_id=thread.id, role="user", content=user_input)

                t0 = time.monotonic()
                run = client.runs.create_and_process(thread_id=thread.id, agent_id=orchestrator.id)
                latency_ms = int((time.monotonic() - t0) * 1000)

                turn_span.set_attribute("run.status", str(run.status))
                turn_span.set_attribute("run.latency_ms", latency_ms)

                if run.last_error:
                    log.error("Run failed", extra={"extra": {"turn": turn, "error": str(run.last_error)}})
                    print("Agent: Sorry, something went wrong. Please try again.\n")
                    continue

                response = get_response_text(client.messages.list(thread_id=thread.id))

                # Output guardrail
                try:
                    screen(response, label="output")
                except GuardrailError as e:
                    turn_span.set_attribute("guardrail.blocked", True)
                    log.warning("Output blocked", extra={"extra": {"turn": turn, "categories": e.categories}})
                    print("Agent: I'm sorry, I can't share that response.\n")
                    continue

                escalated = "ESCALATION_LEVEL:" in response
                turn_span.set_attribute("routing.escalated", escalated)
                log.info("Turn complete", extra={"extra": {
                    "turn": turn,
                    "run_status": str(run.status),
                    "latency_ms": latency_ms,
                    "escalated": escalated,
                }})
                routing_tag = "[ESCALATED]" if escalated else "[KB]"
                print(f"Agent {routing_tag}: {response}\n")

        # Cleanup all three agents
        for agent in [orchestrator, kb_agent, triage_agent]:
            client.delete_agent(agent.id)
        log.info("All agents deleted")
        print("Session ended.")


if __name__ == "__main__":
    main()
