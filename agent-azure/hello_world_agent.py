import os
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool
from azure.identity import DefaultAzureCredential

load_dotenv()


def main():
    client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )

    # Create agent with Code Interpreter
    agent = client.create_agent(
        model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o"),
        name="my-first-agent",
        instructions="You are a helpful assistant.",
        tools=CodeInterpreterTool().definitions,
    )
    print(f"Created agent: {agent.id}")

    # Create a conversation thread
    thread = client.threads.create()

    # Send a message
    client.messages.create(
        thread_id=thread.id,
        role="user",
        content="What is 2 + 2? Show your reasoning.",
    )

    # Run and wait for completion
    run = client.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id,
    )
    print(f"Run status: {run.status}")
    if run.last_error:
        print(f"Error: {run.last_error}")

    # Print the assistant's response
    messages = client.messages.list(thread_id=thread.id)
    for msg in messages:
        if msg.role == "assistant":
            for part in msg.content:
                if hasattr(part, "text"):
                    print(f"\nAssistant: {part.text.value}")

    # Cleanup
    client.threads.delete(thread.id)
    client.delete_agent(agent.id)
    print("Cleaned up.")


if __name__ == "__main__":
    main()
