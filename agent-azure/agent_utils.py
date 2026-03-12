"""
agent_utils.py - Shared Foundry agent utilities.

    run_with_retry(client, thread_id, agent_id, attempts=3)
        Calls create_and_process with exponential-backoff retries on transient failures.
"""

import time

from logger import get_logger

log = get_logger(__name__)


def run_with_retry(client, thread_id: str, agent_id: str, attempts: int = 3):
    """Call create_and_process with exponential-backoff retries on transient failures."""
    for i in range(attempts):
        try:
            return client.runs.create_and_process(thread_id=thread_id, agent_id=agent_id)
        except Exception as exc:
            if i == attempts - 1:
                raise
            log.debug("create_and_process attempt %d failed (%s), retrying in %ds", i + 1, exc, 2 ** i)
            time.sleep(2 ** i)
