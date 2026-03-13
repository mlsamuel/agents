"""
guardrails.py - Content moderation using OpenAI Moderation API.

No API key beyond OPENAI_API_KEY is required.

Public API:
    screen(text, label) -> None   (raises GuardrailError if blocked)
"""

from openai import OpenAI

from logger import get_logger

log = get_logger(__name__)

_client: OpenAI | None = None

# Categories considered blocking. OpenAI moderation returns a flagged bool per category.
_BLOCKING_CATEGORIES = {
    "hate",
    "hate/threatening",
    "harassment",
    "harassment/threatening",
    "self-harm",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual",
    "sexual/minors",
    "violence",
    "violence/graphic",
}


class GuardrailError(Exception):
    """Raised when content moderation blocks a message."""
    def __init__(self, label: str, categories: list[str]):
        self.label = label
        self.categories = categories
        super().__init__(f"Content blocked [{label}]: {', '.join(categories)}")


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def screen(text: str, label: str = "message") -> None:
    """Screen text for harmful content. Raises GuardrailError if blocked.

    Args:
        text: The text to screen.
        label: 'input' or 'output' — used in error messages and logs.
    """
    try:
        response = _get_client().moderations.create(input=text[:4096])
    except Exception as e:
        log.warning("Moderation API error (skipping): %s", e)
        return

    result = response.results[0]
    if not result.flagged:
        return

    cats = result.categories.model_dump()
    blocked = [cat for cat, flagged in cats.items() if flagged and cat in _BLOCKING_CATEGORIES]
    if blocked:
        raise GuardrailError(label, blocked)
