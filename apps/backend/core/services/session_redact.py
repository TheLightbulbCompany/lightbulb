"""Best-effort secret redaction for whatever a session link shows someone outside the
session's workspace: their copy of the transcript and the link's event pages (spec
docs/superpowers/specs/2026-10-05-public-session-links.md §3.4).

Best effort by design: it catches the common shapes of a secret, not every secret. Members
are never redacted (Pick up hands teammates the snapshot as it is); a live screen never can be.
"""

from __future__ import annotations

import re
from typing import Any

from core.services.marketplace_safety import _PATTERNS

MARK = "[redacted]"
PROJECT = "<project>"

# The marketplace's bytes regexes, as str. Their flags are inline ((?ix)), so decoding keeps them.
_REUSED = ("hardcoded-secret", "aws-secret-key", "provider-token")

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MARK),
    *[(re.compile(p.regex.pattern.decode("ascii")), MARK) for p in _PATTERNS if p.name in _REUSED],
    # .env-shaped lines: keep the name, drop the value.
    (
        re.compile(r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*=\s*)\S.*$"),
        rf"\g<1>{MARK}",
    ),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), MARK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MARK),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"), rf"\g<1>{MARK}"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MARK),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), rf"\g<1>{MARK}@"),
    (re.compile(r"/(?:Users|home)/[^/\s\"']+/"), "~/"),
    # The bare home directory (a cwd, a pwd line), with no trailing slash.
    (re.compile(r"/(?:Users|home)/[^/\s\"']+(?=$|[\s\"'`:,;)\]])"), "~"),
]


def redact(text: str, project: str | None = None) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    if project:
        # A private repo's folder name, only where it is a whole path component (after a slash).
        text = re.sub(rf"(?<=/){re.escape(project)}(?=/|$|[\s\"'`:,;)\]])", PROJECT, text)
    return text


def redact_value(value: Any, project: str | None = None) -> Any:
    if isinstance(value, str):
        return redact(value, project)
    if isinstance(value, list):
        return [redact_value(v, project) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, project) for k, v in value.items()}
    return value
