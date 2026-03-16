"""Customer support KB Q&A agent with tracing, logging, and content safety guardrails.

Prerequisites:
    1. Run kb_setup.py once and set VECTOR_STORE_ID in .env
    2. Set CONTENT_SAFETY_ENDPOINT and CONTENT_SAFETY_KEY in .env
    3. Run: python kb_agent.py
"""
import json
import logging
import os
import time
from opentelemetry.trace import SpanKind
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import FileSearchTool
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
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            payload.update(record.extra)
        return json.dumps(payload)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("kb_agent")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False
    return log


SYSTEM_PROMPT = """You are a customer support agent for a software and services company.
Answer questions by searching the knowledge base. Always cite the topic you found the answer in.
If the knowledge base does not contain relevant information, say so clearly and offer to escalate."""


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

    file_search = FileSearchTool(vector_store_ids=[vector_store_id])

    with tracer.start_as_current_span("agent-session", kind=SpanKind.SERVER) as session_span:
        # Create agent
        agent = client.create_agent(
            model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o"),
            name="kb-support-agent",
            instructions=SYSTEM_PROMPT,
            tools=file_search.definitions,
            tool_resources=file_search.resources,
        )
        session_span.set_attribute("agent.id", agent.id)
        log.info("Agent created", extra={"extra": {"agent_id": agent.id}})

        # Create a persistent thread for the conversation
        thread = client.threads.create()
        session_span.set_attribute("thread.id", thread.id)
        log.info("Thread created", extra={"extra": {"thread_id": thread.id}})

        print("\nCustomer Support KB Agent")
        print("=" * 40)
        print("Ask a question about billing, returns, technical support, or general info.")
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
            with tracer.start_as_current_span("turn", kind=SpanKind.CONSUMER) as turn_span:
                turn_span.set_attribute("turn", turn)

                # --- Input guardrail ---
                try:
                    screen(user_input, label="input")
                except GuardrailError as e:
                    turn_span.set_attribute("guardrail.blocked", True)
                    turn_span.set_attribute("guardrail.label", e.label)
                    log.warning("Input blocked by guardrail", extra={"extra": {
                        "turn": turn, "categories": e.categories
                    }})
                    print(f"Agent: I'm sorry, I can't process that request.\n")
                    continue

                # --- Send message and run ---
                client.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=user_input,
                )

                t0 = time.monotonic()
                run = client.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=agent.id,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                turn_span.set_attribute("run.status", str(run.status))
                turn_span.set_attribute("run.latency_ms", latency_ms)

                if run.last_error:
                    log.error("Run failed", extra={"extra": {
                        "turn": turn, "error": str(run.last_error)
                    }})
                    print(f"Agent: Sorry, something went wrong. Please try again.\n")
                    continue

                response = get_response_text(
                    client.messages.list(thread_id=thread.id)
                )

                # --- Output guardrail ---
                try:
                    screen(response, label="output")
                except GuardrailError as e:
                    turn_span.set_attribute("guardrail.blocked", True)
                    log.warning("Output blocked by guardrail", extra={"extra": {
                        "turn": turn, "categories": e.categories
                    }})
                    print(f"Agent: I'm sorry, I can't share that response.\n")
                    continue

                log.info("Turn complete", extra={"extra": {
                    "turn": turn,
                    "run_status": str(run.status),
                    "latency_ms": latency_ms,
                    "input_chars": len(user_input),
                    "output_chars": len(response),
                }})

                print(f"Agent: {response}\n")

        # Cleanup agent (keep thread + vector store persistent for demo)
        client.delete_agent(agent.id)
        log.info("Agent deleted", extra={"extra": {"agent_id": agent.id}})
        print("Session ended.")


if __name__ == "__main__":
    main()
