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
        "provider-token",
        "high",
        # Bare provider-prefixed tokens (mirrors _PROVIDER_KEY_PREFIXES below):
        # OpenAI/Anthropic sk-*, GitHub ghp_/gho_/ghs_/github_pat_, Slack
        # xoxb-/xoxp-/xapp-. The hardcoded-secret pattern only fires on
        # `key = "..."` assignments; a token pasted bare in prose or args
        # needs its own net.
        re.compile(
            rb"\b(?:"
            rb"sk-[A-Za-z0-9_-]{20,}"
            rb"|(?:ghp|gho|ghs)_[A-Za-z0-9]{20,}"
            rb"|github_pat_[A-Za-z0-9_]{20,}"
            rb"|x(?:oxb|oxp|app)-[A-Za-z0-9-]{10,}"
            rb")"
        ),
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


# ---------------------------------------------------------------------------
# Post-scrub credential-leak check on the published openclaw-slice.json
#
# WHY this exists separately from scan(): the slice is a SEPARATE S3 artifact
# (openclaw-slice.json) built by catalog_slice from the seller's
# mcp.servers / skills.entries with secret-shaped fields scrubbed. The
# tarball-only safety scan (scan() above) NEVER walks the slice — so a
# credential that survives the scrub (because the scrub is shallow/one-level,
# or the field is non-canonical, or an OAuth client-secret is buried in a
# string) would ship in the published artifact undetected. These rules
# re-inspect the scrubbed slice and block publish if anything leaked.
#
# Severity convention mirrors the credential patterns above (hardcoded-secret,
# aws-secret-key): credential leaks are always "high".
# ---------------------------------------------------------------------------

# The filename surfaced on slice-derived flags. The slice is a distinct
# artifact, so its flags don't share the tarball's file paths.
_SLICE_FILE = "openclaw-slice.json"

# Pattern names for the two slice-leak classes. has_blocking_credential_findings
# keys off this set, so adding a new high-severity slice rule means adding its
# name here too.
_BLOCKING_CREDENTIAL_PATTERNS = frozenset({"unscrubbed-credential", "oauth-client-secret", "arg-secret-suspected"})

# OAuth client-secret literal keys. A scrub keyed on the canonical sensitive
# field names (apiKey/env/headers/webhookToken) won't touch a config that
# stores the OAuth client secret under its own key, so detect it by name.
_CLIENT_SECRET_KEY_RE = re.compile(r"client[_-]?secret", re.IGNORECASE)

# Secret CLI flags whose following value is a credential (mirrors catalog_slice).
_SECRET_ARG_FLAGS = frozenset({"--api-key", "--apikey", "--key", "--token", "--secret", "--password"})

# High-confidence credential prefixes.
_PROVIDER_KEY_PREFIXES = (
    "sk-",
    "sk-ant-",
    "ghp_",
    "gho_",
    "ghs_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xapp-",
)
_AWS_KEY_RE = re.compile(r"^(?:AKIA|ASIA)[0-9A-Z]{16}$")
# npm package spec shape — NOT a secret (e.g. "@perplexity-ai/mcp-server", "instantly-mcp").
_NPM_PACKAGE_RE = re.compile(r"^@?[a-z0-9][a-z0-9._/-]*$")
# Generic high-entropy token: >=40 base64/hex-ish chars.
_HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def _looks_like_arg_secret(value: str) -> bool:
    """True for a standalone arg that looks like a credential, excluding npm
    package specs. Conservative: high-confidence prefixes, AWS keys, or a long
    high-entropy mixed-case+digit token.

    Intentionally narrow: a credential that is neither a known-prefix token nor
    long-and-high-entropy (e.g. short Stripe/Google-style keys, all-lowercase-hex
    tokens) is NOT caught when it appears as a bare positional. The flag-value
    path (a value following ``--api-key`` etc.) is the primary net; this
    standalone check is a secondary backstop tuned to avoid false-blocking
    legitimate package specs and paths, so the false-negatives here are the
    deliberate cost of that low false-positive rate.
    """
    if not isinstance(value, str) or value == "":
        return False
    if any(value.startswith(p) for p in _PROVIDER_KEY_PREFIXES):
        return True
    if _AWS_KEY_RE.match(value):
        return True
    if _NPM_PACKAGE_RE.match(value):
        return False
    return bool(
        _HIGH_ENTROPY_RE.match(value)
        and any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and any(c.isdigit() for c in value)
    )


def _walk_for_arg_secrets(slice_: dict) -> list[tuple[str, str]]:
    """(path, value) for credential-looking ``args`` across mcp.servers /
    skills.entries: a non-empty value after a secret flag, a non-empty
    ``--flag=value``, or a standalone credential-shaped token."""
    found: list[tuple[str, str]] = []
    for path, entry in _walk_slice_entries(slice_):
        args = entry.get("args")
        if not isinstance(args, list):
            continue
        expect_value_for_flag = False
        for i, a in enumerate(args):
            if not isinstance(a, str):
                expect_value_for_flag = False
                continue
            if expect_value_for_flag:
                if a != "":
                    found.append((f"{path}.args[{i}]", a))
                expect_value_for_flag = False
                continue
            if a.lower() in _SECRET_ARG_FLAGS:
                expect_value_for_flag = True
                continue
            eq = a.find("=")
            if eq != -1 and a[:eq].lower() in _SECRET_ARG_FLAGS:
                if a[eq + 1 :] != "":
                    found.append((f"{path}.args[{i}]", a[eq + 1 :]))
                continue
            if _looks_like_arg_secret(a):
                found.append((f"{path}.args[{i}]", a))
    return found


def _redact(value: str) -> str:
    """Redacted snippet for a leaked secret. NEVER echo the raw value — the
    flag rides into the manifest / admin UI / logs. Show only the length and
    a 2-char prefix so a reviewer can correlate without the secret leaking
    again through the very mechanism meant to catch it.
    """
    prefix = value[:2]
    return f"<redacted len={len(value)} prefix={prefix!r}>"


def _first_nonempty_string(value: object) -> str | None:
    """First non-empty string found by the same deep walk, for the snippet.
    Returns None when nothing leaked.
    """
    if isinstance(value, str):
        return value if value != "" else None
    if isinstance(value, dict):
        for v in value.values():
            found = _first_nonempty_string(v)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            found = _first_nonempty_string(v)
            if found is not None:
                return found
        return None
    return None


def _walk_slice_entries(slice_: dict) -> Iterable[tuple[str, dict]]:
    """Yield (path, entry_dict) for each mcp.servers.*, skills.entries.* and
    plugins.entries.* entry in the slice. ``path`` is a human-readable dotted
    location used in the flag snippet (no secret in it). Non-dict entries are
    skipped — the scrub only ever produced dicts, and a stray non-dict can't
    carry a secret-shaped field.
    """
    mcp_servers = (slice_.get("mcp") or {}).get("servers") or {}
    if isinstance(mcp_servers, dict):
        for name, entry in mcp_servers.items():
            if isinstance(entry, dict):
                yield f"mcp.servers.{name}", entry

    skills = slice_.get("skills") or {}
    entries = skills.get("entries") if isinstance(skills, dict) else None
    if isinstance(entries, dict):
        for name, entry in entries.items():
            if isinstance(entry, dict):
                yield f"skills.entries.{name}", entry

    # Plugin entries carry the same apiKey/env-shaped config as MCP servers;
    # catalog_slice scrubs them the same way, so the blocking gate must watch
    # them the same way too.
    plugins = slice_.get("plugins") or {}
    plugin_entries = plugins.get("entries") if isinstance(plugins, dict) else None
    if isinstance(plugin_entries, dict):
        for name, entry in plugin_entries.items():
            if isinstance(entry, dict):
                yield f"plugins.entries.{name}", entry


def _walk_for_client_secret(value: object, path: str) -> list[tuple[str, str]]:
    """Deep-walk ``value`` collecting (path, secret) pairs for every key that
    matches ``client_secret`` / ``clientSecret`` (case-insensitive) with a
    non-empty string value, ANYWHERE in the structure. The scrub never targets
    these keys, so a literal OAuth client secret rides through untouched.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            child_path = f"{path}.{k}"
            if isinstance(k, str) and _CLIENT_SECRET_KEY_RE.fullmatch(k) and isinstance(v, str) and v != "":
                found.append((child_path, v))
            found.extend(_walk_for_client_secret(v, child_path))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.extend(_walk_for_client_secret(v, f"{path}[{i}]"))
    return found


def _walk_for_secret_fields(value: object, path: str, field_names) -> list[tuple[str, str]]:
    """Deep-walk ``value`` collecting (path, leaked_value) for every key in
    ``field_names`` whose value contains a non-empty string ANYWHERE inside it.

    Recurses through nested dicts/lists, so a secret-shaped key buried under a
    non-secret object (e.g. ``mcp.servers.foo.config.apiKey`` or
    ``skills.entries.bar.options.headers.Authorization``) is caught. The shallow
    scrub only blanks these names at the TOP level of an entry, so a nested one
    survives both the scrub and any top-level-only check.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            child_path = f"{path}.{k}"
            if isinstance(k, str) and k in field_names:
                leaked = _first_nonempty_string(v)
                if leaked is not None:
                    found.append((child_path, leaked))
            found.extend(_walk_for_secret_fields(v, child_path, field_names))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.extend(_walk_for_secret_fields(v, f"{path}[{i}]", field_names))
    return found


def assert_slice_scrubbed(openclaw_slice: dict) -> list[SafetyFlag]:
    """Re-check a (supposedly-scrubbed) openclaw-slice for surviving secrets.

    Walks every ``mcp.servers.*``, ``skills.entries.*`` and
    ``plugins.entries.*`` entry and returns
    a HIGH-severity flag for each credential that survived
    ``catalog_slice._scrub_secret_fields``:

      - ``unscrubbed-credential`` — a secret-shaped field (a key in
        ``catalog_slice._SECRET_FIELD_NAMES``) whose value contains a
        non-empty string ANYWHERE inside it. The deep walk catches nested
        leaks the shallow one-level scrub misses.
      - ``oauth-client-secret`` — any ``client_secret`` / ``clientSecret``
        key (case-insensitive) with a non-empty value, anywhere in the slice.

    Returns ``[]`` for a properly-scrubbed slice. The flag snippet is
    REDACTED — the raw secret never appears in it (see ``_redact``).
    """
    # Import here (not module-top) to keep the field-name list single-sourced
    # in catalog_slice without creating an import cycle at module load.
    from core.services.catalog_slice import _SECRET_FIELD_NAMES

    flags: list[SafetyFlag] = []

    for path, entry in _walk_slice_entries(openclaw_slice):
        # Walk the WHOLE entry, not just its top-level fields: a secret-named key
        # (apiKey/env/headers/webhookToken) nested under a non-secret object
        # survives the shallow scrub, so a top-level-only check would miss it.
        for child_path, leaked in _walk_for_secret_fields(entry, path, _SECRET_FIELD_NAMES):
            flags.append(
                SafetyFlag(
                    pattern="unscrubbed-credential",
                    severity="high",
                    file=_SLICE_FILE,
                    line=None,
                    snippet=f"{child_path} = {_redact(leaked)}",
                )
            )

    for child_path, secret in _walk_for_client_secret(openclaw_slice, ""):
        flags.append(
            SafetyFlag(
                pattern="oauth-client-secret",
                severity="high",
                file=_SLICE_FILE,
                line=None,
                snippet=f"{child_path.lstrip('.')} = {_redact(secret)}",
            )
        )

    for child_path, secret in _walk_for_arg_secrets(openclaw_slice):
        flags.append(
            SafetyFlag(
                pattern="arg-secret-suspected",
                severity="high",
                file=_SLICE_FILE,
                line=None,
                snippet=f"{child_path} = {_redact(secret)}",
            )
        )

    return flags


def has_blocking_credential_findings(flags: Iterable[SafetyFlag]) -> bool:
    """True if any flag is one of the high-severity slice-credential leak
    types. Publish is blocked when this returns True.
    """
    return any(f.pattern in _BLOCKING_CREDENTIAL_PATTERNS and f.severity == "high" for f in flags)


# ---------------------------------------------------------------------------
# Publishable (snippet-free) flag shapes
#
# ``snippet`` is 80 raw characters of the seller's own source line. By
# construction it is the field MOST likely to hold a credential — finding
# secret-shaped strings is the scanner's entire job — and it also discloses a
# private workspace's file paths + line numbers.
#
# It may reach LIVE, authenticated surfaces (the seller's publish preview,
# admin review), which re-scan the artifact on demand. It must NEVER be
# persisted into an artifact manifest: manifests are written to S3 and onto
# the DynamoDB listing row, and both are served verbatim by the
# UNAUTHENTICATED listing-detail + search routes under a public cache header.
# ---------------------------------------------------------------------------


def flags_without_snippets(flags: Iterable[dict]) -> list[dict]:
    """Flag dicts with ``snippet`` dropped — the persistable/public shape."""
    return [{k: v for k, v in f.items() if k != "snippet"} for f in flags if isinstance(f, dict)]


def manifest_without_snippets(manifest):
    """``manifest`` with every ``safety_flags[].snippet`` dropped.

    Returns the SAME object when there is nothing to strip, so read paths can
    skip copying rows that are already clean. Non-dict input passes through
    (the listing-detail route tolerates a null/garbage manifest).

    This is the read-side half of the fix: published artifacts are immutable,
    so manifests written before the write-side strip landed keep their
    snippets until the one-shot scrub script (deleted 2026-09-11; see git
    history) rewrites them.
    """
    if not isinstance(manifest, dict):
        return manifest
    flags = manifest.get("safety_flags")
    if not isinstance(flags, list) or not any(isinstance(f, dict) and "snippet" in f for f in flags):
        return manifest
    return {**manifest, "safety_flags": flags_without_snippets(flags)}
