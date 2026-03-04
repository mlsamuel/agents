"""
client.py - Thin Anthropic client wrapper with exponential backoff.

Usage:
    from client import Client
    client = Client()
    response = client.messages.create(model=..., max_tokens=..., messages=...)
"""

import random
import time

import anthropic

from logger import get_logger

log = get_logger(__name__)

MAX_RETRIES = 6
BASE_DELAY = 1.0   # seconds
MAX_DELAY = 60.0   # seconds

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


class _Messages:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._inner.create(**kwargs)
            except anthropic.APIStatusError as exc:
                if exc.status_code not in _RETRYABLE_STATUS or attempt == MAX_RETRIES:
                    raise
                delay = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 1), MAX_DELAY)
                log.warning(
                    "API error %d (attempt %d/%d), retrying in %.1fs",
                    exc.status_code, attempt + 1, MAX_RETRIES, delay,
                )
            except anthropic.APIConnectionError:
                if attempt == MAX_RETRIES:
                    raise
                delay = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 1), MAX_DELAY)
                log.warning(
                    "API connection error (attempt %d/%d), retrying in %.1fs",
                    attempt + 1, MAX_RETRIES, delay,
                )
            time.sleep(delay)


class Client:
    """Drop-in replacement for anthropic.Anthropic() with built-in retry."""

    def __init__(self):
        _raw = anthropic.Anthropic()
        self.messages = _Messages(_raw.messages)
