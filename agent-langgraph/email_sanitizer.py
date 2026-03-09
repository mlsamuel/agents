"""
email_sanitizer.py - Layer 3: Pattern stripping for prompt injection defence.

Strips or neutralises strings from email subject/body that exploit the structural
conventions LLM prompts rely on (role headers, instruction overrides, fake tool
output, XML control tags, etc.).  This is a cheap, zero-API-cost first pass that
removes the most obvious attack vectors before the email touches any model.
"""

import re

# ── Patterns to replace with a safe placeholder ───────────────────────────────

_REPLACEMENT = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Explicit instruction overrides
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
                re.IGNORECASE), _REPLACEMENT),
    (re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
                re.IGNORECASE), _REPLACEMENT),
    (re.compile(r"forget\s+(everything|all)\s+(you('ve| have)\s+)?been\s+told",
                re.IGNORECASE), _REPLACEMENT),
    (re.compile(r"your\s+(new\s+)?(instructions?|prompt|system\s+prompt)\s+(is|are)\s*:",
                re.IGNORECASE), _REPLACEMENT),
    (re.compile(r"act\s+as\s+(if\s+you\s+(are|were)|a\s+)",
                re.IGNORECASE), _REPLACEMENT),

    # Role / turn header spoofing
    (re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE), _REPLACEMENT + ":"),
    (re.compile(r"^\s*assistant\s*:", re.IGNORECASE | re.MULTILINE), _REPLACEMENT + ":"),
    (re.compile(r"^\s*human\s*:", re.IGNORECASE | re.MULTILINE), _REPLACEMENT + ":"),
    (re.compile(r"^\s*user\s*:", re.IGNORECASE | re.MULTILINE), _REPLACEMENT + ":"),

    # XML control tag injection (strip the tags, keep any innocent inner text)
    (re.compile(r"<\s*/?\s*(system|assistant|tool_result|tool_use|human)\b[^>]*>",
                re.IGNORECASE), ""),

    # Fake tool / function call syntax
    (re.compile(r"\bfunction_call\s*\(", re.IGNORECASE), _REPLACEMENT + "("),
    (re.compile(r"\btool_call\s*\(", re.IGNORECASE), _REPLACEMENT + "("),

    # Prompt-boundary markers commonly used in datasets / fine-tuning
    (re.compile(r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>"), ""),
    (re.compile(r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>"), ""),

    # Markdown heading tricks ("## New Instructions")
    (re.compile(r"^#{1,4}\s*(new\s+)?(instructions?|system(\s+prompt)?|prompt|override)",
                re.IGNORECASE | re.MULTILINE), _REPLACEMENT),
]


def _apply_patterns(text: str) -> tuple[str, list[str]]:
    """Apply all patterns, return cleaned text and list of what was stripped."""
    stripped: list[str] = []
    for pattern, replacement in _PATTERNS:
        new_text, n = pattern.subn(replacement, text)
        if n:
            stripped.append(pattern.pattern[:60])
            text = new_text
    return text, stripped


def sanitize(email: dict) -> tuple[dict, list[str]]:
    """
    Return a sanitized copy of the email dict and a list of patterns that were stripped.

    Usage:
        clean_email, warnings = sanitize(email)
        if warnings:
            log(f"Stripped injection patterns: {warnings}")
    """
    clean = dict(email)
    all_warnings: list[str] = []

    for field in ("subject", "body"):
        val = clean.get(field)
        if isinstance(val, str):
            clean[field], warnings = _apply_patterns(val)
            all_warnings.extend(warnings)

    return clean, all_warnings
