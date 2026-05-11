"""Regex-based safety scan for marketplace listing artifacts.

Best-effort. v1 uses bytes-level regex; AST scan + symbolic interpretation
deferred. Goal is to surface obvious red flags so admins moderating large
queues don't rubber-stamp listings that contain `curl … | bash`, hardcoded
keys, or similar trivially-detectable trouble.

Admins still make the final call. High-severity findings pre-fill the
rejection-notes textarea on the admin UI; medium/low findings render as
informational.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal


Severity = Literal["high", "medium", "low"]


@dataclass
class SafetyFlag:
    pattern: str
    severity: Severity
    file: str
    line: int | None
    snippet: str


@dataclass
class _Pattern:
    name: str
    severity: Severity
    regex: re.Pattern[bytes]
    applies_to: tuple[str, ...] = ("skillmd", "openclaw", "persona")


# Patterns. Names are short identifiers used in audit-log and UI surfaces.
# applies_to defaults to all three formats; code-injection / write-side
# patterns scope down to "openclaw" only, and PII-shaped patterns scope
# down to "persona" only.
_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        "curl-bash",
        "high",
        re.compile(rb"(?:curl|wget)\s+[^|\n]+\|\s*(?:bash|sh|zsh|ksh)\b"),
        applies_to=("skillmd", "openclaw"),
    ),
    _Pattern(
        "eval",
        "high",
        re.compile(rb"\beval\s*\("),
        applies_to=("skillmd", "openclaw"),
    ),
    _Pattern(
        "function-ctor",
        "high",
        re.compile(rb"\bnew\s+Function\s*\("),
        applies_to=("skillmd", "openclaw"),
    ),
    _Pattern(
        "exec",
        "high",
        re.compile(rb"\b(?:os\.system|subprocess\.(?:run|Popen|call|check_call|check_output))\s*\("),
        applies_to=("skillmd", "openclaw"),
    ),
    _Pattern(
        "child-process-shell",
        "high",
        re.compile(
            rb"child_process\.(?:exec|execSync|spawn(?:Sync)?)\s*\([^)]*shell\s*[:=]\s*true",
        ),
        applies_to=("skillmd", "openclaw"),
    ),
    # Hardcoded credentials. Tolerates underscore/dash variants and matches
    # quoted values of 16+ alphanumeric/underscore/dash chars. Applies to
    # persona files too — sellers sometimes paste API keys into MEMORY.md.
    _Pattern(
        "hardcoded-secret",
        "high",
        re.compile(
            rb"""(?ix)
            (?:api[_-]?key|secret|access[_-]?token|password|bearer|auth[_-]?token)
            \s*[:=]\s*['\"][a-z0-9_\-]{16,}['\"]
            """
        ),
    ),
    _Pattern(
        "aws-secret-key",
        "high",
        # AWS access keys are AKIA/ASIA + 16 alphanum; secret keys are 40 base64
        # chars. Match the access-key form (very low false-positive rate).
        re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    _Pattern(
        "process-env-write",
        "medium",
        re.compile(rb"process\.env\.[A-Z_]+\s*="),
        applies_to=("openclaw",),
    ),
    _Pattern(
        "fs-write-absolute",
        "medium",
        # fs.writeFileSync('/etc/...') style writes outside relative paths.
        re.compile(rb"""fs\.(?:writeFileSync|writeFile|appendFileSync|appendFile)\s*\(\s*['"]/[^'"\n]+['"]"""),
        applies_to=("openclaw",),
    ),
    _Pattern(
        "outbound-fetch-non-allowlisted",
        "medium",
        # Fetches to suspicious hosts. Conservative — only flags pastebin/IP
        # addresses, which buy almost nothing legitimate in a skill.
        re.compile(
            rb"""(?ix)
            (?:fetch|axios\.get|axios\.post|requests\.(?:get|post)|httpx\.(?:get|post))
            \s*\(\s*['"]https?://
            (?:
                pastebin\.com
              | rawpaste\.com
              | hastebin\.com
              | bit\.ly
              | tinyurl\.com
              | (?:\d{1,3}\.){3}\d{1,3}     # IPv4 literals
            )
            """
        ),
        applies_to=("skillmd", "openclaw"),
    ),
    # ----- Persona-file PII patterns (format="persona") -----
    # Trust-seller posture: we flag, the seller redacts. Same regex spirit
    # as the env-var leak detection used elsewhere — surface for review.
    _Pattern(
        "email-address",
        "medium",
        re.compile(rb"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        applies_to=("persona",),
    ),
    _Pattern(
        "phone-number",
        "low",
        # International / NANP phone numbers. Liberal but anchored on a
        # leading + or area-code group to keep false-positive rate low.
        re.compile(rb"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}"),
        applies_to=("persona",),
    ),
)


_LARGE_BINARY_BYTES = 1 * 1024 * 1024  # 1 MB
_BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp3",
    ".mp4",
    ".wav",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bin",
)


def _is_binary(data: bytes) -> bool:
    # Cheap heuristic: presence of NUL byte in first 8 KB.
    return b"\x00" in data[:8192]


def _line_for_offset(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _snippet(data: bytes, start: int, end: int, max_len: int = 80) -> str:
    line_start = data.rfind(b"\n", 0, start) + 1
    line_end = data.find(b"\n", end)
    if line_end == -1:
        line_end = len(data)
    raw = data[line_start:line_end]
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def scan(file_dict: dict[str, bytes], format: str) -> list[SafetyFlag]:
    """Scan a file map (path → bytes) for suspicious content.

    Args:
        file_dict: ordered map of relative paths to raw bytes.
        format: "skillmd" or "openclaw" — gates patterns that only apply
            to one or the other.

    Returns:
        List of SafetyFlag, ordered by severity (high → medium → low) then
        by file path.
    """
    flags: list[SafetyFlag] = []

    for path, data in file_dict.items():
        path_lower = path.lower()
        is_binary = _is_binary(data) or any(path_lower.endswith(suf) for suf in _BINARY_SUFFIXES)

        if is_binary:
            if len(data) > _LARGE_BINARY_BYTES:
                flags.append(
                    SafetyFlag(
                        pattern="large-binary",
                        severity="low",
                        file=path,
                        line=None,
                        snippet=f"binary file {len(data)} bytes",
                    )
                )
            # Don't run text patterns on binaries.
            continue

        for pat in _PATTERNS:
            if format not in pat.applies_to:
                continue
            for m in pat.regex.finditer(data):
                flags.append(
                    SafetyFlag(
                        pattern=pat.name,
                        severity=pat.severity,
                        file=path,
                        line=_line_for_offset(data, m.start()),
                        snippet=_snippet(data, m.start(), m.end()),
                    )
                )

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (severity_rank[f.severity], f.file, f.line or 0))
    return flags


def to_rejection_notes(flags: Iterable[SafetyFlag]) -> str:
    """Format high-severity flags as rejection-note pre-fill copy."""
    high = [f for f in flags if f.severity == "high"]
    if not high:
        return ""
    lines = ["Auto-flagged by safety scan:"]
    for f in high[:10]:  # cap to keep textarea manageable
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"- {f.pattern} at {loc}: {f.snippet}")
    if len(high) > 10:
        lines.append(f"… and {len(high) - 10} more high-severity findings")
    return "\n".join(lines)
