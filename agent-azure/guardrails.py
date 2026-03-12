import os
from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential


class GuardrailError(Exception):
    """Raised when content safety screening blocks a message."""
    def __init__(self, label: str, categories: list[str]):
        self.label = label
        self.categories = categories
        super().__init__(f"Content blocked [{label}]: {', '.join(categories)}")


_client: ContentSafetyClient | None = None

# Block severity >= 4 (medium). Azure uses 0-7 scale.
SEVERITY_THRESHOLD = 4

CATEGORIES = [
    TextCategory.HATE,
    TextCategory.VIOLENCE,
    TextCategory.SELF_HARM,
    TextCategory.SEXUAL,
]


def _get_client() -> ContentSafetyClient:
    global _client
    if _client is None:
        _client = ContentSafetyClient(
            os.environ["CONTENT_SAFETY_ENDPOINT"],
            DefaultAzureCredential(),
        )
    return _client


def screen(text: str, label: str = "message") -> None:
    """Screen text for harmful content. Raises GuardrailError if blocked.

    Args:
        text: The text to screen.
        label: 'input' or 'output' — used in error messages and logs.
    """
    client = _get_client()
    try:
        response = client.analyze_text(AnalyzeTextOptions(text=text, categories=CATEGORIES))
    except HttpResponseError as e:
        # Don't block the conversation if Content Safety is unavailable
        import logging
        logging.getLogger(__name__).warning("Content Safety API error (skipping): %s", e)
        return

    blocked = [
        r.category
        for r in response.categories_analysis
        if r.severity is not None and r.severity >= SEVERITY_THRESHOLD
    ]

    if blocked:
        raise GuardrailError(label, blocked)
