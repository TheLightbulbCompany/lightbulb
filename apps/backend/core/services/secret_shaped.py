"""Credential-shaped text, one definition for every memory write path: the evaluator keeps a
finding author-only, and ``memory_api.remember`` refuses before anything is sent or queued.
``SECRET_PATTERNS`` is verbatim from the CLI's refusal list (src/memory.rs, cli main 5b2e2c5 + #85),
so a refusal here names the same ``secret:<name>`` the cloud member would."""

import re

KEYWORD = re.compile(r"secret|credential|token|\bkeys?\b|api[_ -]?key|password", re.I)
SECRET_PATTERNS = [
    (name, re.compile(rx))
    for name, rx in (
        ("credential", r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S{8,}"),
        ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("aws_key", r"AKIA[0-9A-Z]{16}"),
        ("api_key", r"sk-[A-Za-z0-9_-]{20,}"),
        (
            "vendor_key",
            r"\b(?:[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
            r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20})",
        ),
        ("url_credentials", r"https?://[^\s/]+:[^\s@]+@"),
        ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    )
]


def secret_pattern(text: str) -> str | None:
    """The name of the first CLI pattern ``text`` matches, or None."""
    return next((name for name, rx in SECRET_PATTERNS if rx.search(text)), None)


def secret_shaped(text: str) -> bool:
    """A secret keyword or a credential-shaped string."""
    return bool(KEYWORD.search(text)) or secret_pattern(text) is not None


def redact(text: str) -> str:
    """``text`` with every credential-shaped match replaced by ``[REDACTED]`` (keywords alone stay)."""
    for _, rx in SECRET_PATTERNS:
        text = rx.sub("[REDACTED]", text)
    return text
