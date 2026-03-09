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

# Cost per million tokens (input, output) — update when pricing changes
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-opus-4-6":           (15.00, 75.00),
}


class _Messages:
    def __init__(self, inner, counter: "Client"):
        self._inner = inner
        self._counter = counter

    def create(self, **kwargs):
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._inner.create(**kwargs)
                model = kwargs.get("model", "")
                usage = response.usage
                bucket = self._counter._usage.setdefault(model, [0, 0])
                bucket[0] += usage.input_tokens
                bucket[1] += usage.output_tokens
                return response
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
    """Drop-in replacement for anthropic.Anthropic() with built-in retry and cost tracking."""

    def __init__(self):
        self._usage: dict[str, list[int]] = {}  # model → [input_tokens, output_tokens]
        _raw = anthropic.Anthropic()
        self.messages = _Messages(_raw.messages, self)

    def cost_usd(self) -> float:
        """Return estimated total cost in USD based on per-model token usage."""
        total = 0.0
        for model, (inp, out) in self._usage.items():
            prices = _PRICING.get(model)
            if prices:
                total += (inp * prices[0] + out * prices[1]) / 1_000_000
        return total

    def usage_summary(self) -> str:
        """Return a one-line summary of token usage and estimated cost."""
        total_in  = sum(v[0] for v in self._usage.values())
        total_out = sum(v[1] for v in self._usage.values())
        return (f"tokens: {total_in:,} in / {total_out:,} out  "
                f"cost: ~${self.cost_usd():.4f}")
