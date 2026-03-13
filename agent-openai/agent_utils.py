"""
agent_utils.py - OpenAI Assistants run execution utilities.

    run_with_tool_dispatch(client, thread_id, assistant_id, tool_fns, attempts=3)
        Creates an Assistants run, dispatches function tool calls automatically,
        and polls to completion. Retries on transient failures.

    run_simple(client, system, user_msg, model, response_format=None) -> str
        Single-turn Chat Completions call. Used by classifier, evaluator, improver.
"""

import json
import time

from openai import OpenAI

from logger import get_logger

log = get_logger(__name__)


def run_with_tool_dispatch(
    client: OpenAI,
    thread_id: str,
    assistant_id: str,
    tool_fns: dict,
    attempts: int = 3,
):
    """Create an Assistants run and handle function tool calls until completion.

    When the run enters `requires_action` state (the model has called one or more
    function tools), the matching Python functions are called and their outputs
    submitted back. This loop repeats until the run reaches a terminal state.

    Args:
        client: OpenAI client instance.
        thread_id: ID of the thread to run against.
        assistant_id: ID of the assistant to use.
        tool_fns: {name: callable} — Python functions to dispatch tool calls to.
        attempts: Number of retries on transient API errors.

    Returns:
        The completed run object.
    """
    for attempt in range(attempts):
        try:
            run = client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=assistant_id,
            )
            break
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            log.debug("run create attempt %d failed (%s), retrying in %ds", attempt + 1, exc, 2 ** attempt)
            time.sleep(2 ** attempt)

    while run.status in ("queued", "in_progress", "requires_action"):
        if run.status == "requires_action":
            tool_calls = run.required_action.submit_tool_outputs.tool_calls
            tool_outputs = []
            for tc in tool_calls:
                fn = tool_fns.get(tc.function.name)
                if fn:
                    try:
                        args = json.loads(tc.function.arguments)
                        output = fn(**args)
                    except Exception as e:
                        output = json.dumps({"error": str(e)})
                else:
                    output = json.dumps({"error": f"unknown tool: {tc.function.name}"})
                log.debug("tool_dispatch → %s → %s", tc.function.name, str(output)[:120])
                tool_outputs.append({"tool_call_id": tc.id, "output": str(output)})

            run = client.beta.threads.runs.submit_tool_outputs_and_poll(
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs,
            )
        else:
            run = client.beta.threads.runs.poll(thread_id=thread_id, run_id=run.id)

    return run


def run_simple(
    client: OpenAI,
    system: str,
    user_msg: str,
    model: str,
    response_format: dict | None = None,
) -> str:
    """Single-turn Chat Completions call. Returns the assistant message content.

    Used for stateless single-call operations: classify, judge, improve.
    Lighter than Assistants (no thread/assistant lifecycle management).
    """
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }
    if response_format:
        kwargs["response_format"] = response_format

    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""
