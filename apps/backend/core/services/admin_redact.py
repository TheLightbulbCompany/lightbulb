"""Recursive openclaw.json secret-field redactor.

Per CEO review S3 (#351): the admin agent-detail page renders a user's
openclaw.json config inline, but that config can contain BYOK provider
keys, webhook URLs, and other secrets. Strip them before serializing
to the admin frontend.

Pattern allowlist (case-insensitive on keys):
- *_key, *_secret, *_token, *_password
- webhook_url, api_key, bearer

Values get replaced with the literal string "***redacted***". Keys
themselves stay (so the admin can see "anthropic_api_key was set"
without seeing the value).

Note: this is the deep-recursive redactor for nested config dicts.
The lighter `_redact_payload` in admin_audit.py is a shallow top-level
redact — different use case (audit row payload vs full config tree).
"""

import re
from typing import Any

_REDACT_PATTERNS = [
    re.compile(r"_key$"),
    re.compile(r"_secret$"),
    re.compile(r"_token$"),
    re.compile(r"_password$"),
    re.compile(r"^webhook_url$"),
    re.compile(r"^api_key$"),
    re.compile(r"^bearer$"),
]

REDACTED_VALUE = "***redacted***"


def _should_redact(key: str) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(pattern.search(lowered) for pattern in _REDACT_PATTERNS)


def redact_openclaw_config(value: Any) -> Any:
    """Walk a JSON-shaped value and redact any sensitive leaves.

    Preserves shape — dict keys remain, list ordering remains, scalar
    values pass through. Only values whose dict-key matches the
    redact patterns get replaced.

    Idempotent: redacting an already-redacted structure is a no-op.
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED_VALUE if _should_redact(key) else redact_openclaw_config(child))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_openclaw_config(item) for item in value]
    return value
