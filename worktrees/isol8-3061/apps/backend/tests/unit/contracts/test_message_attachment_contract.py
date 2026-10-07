"""Cross-language conformance test for the message-attachment allowlist.

The set of attachable types and their per-type size caps is written out FOUR times,
in two languages, with nothing but a hand-written "Mirrors the backend" comment holding
them together:

  1. ``MessageAttachment.content_type``'s ``Literal`` -- the wire contract
  2. ``ATTACHMENT_MAX_BYTES_BY_TYPE``                 -- the per-type cap
  3. ``_ATTACHMENT_SIGNATURES``                       -- the magic-byte check
  4. ``MESSAGE_ATTACHMENT_CONTENT_TYPES`` / ``MESSAGE_ATTACHMENT_MAX_BYTES`` /
     ``MESSAGE_ATTACHMENT_MAX_COUNT`` in ``packages/shared/src/messaging/types.ts``
     -- what the composer will hand to the upload route in the first place

Drift between (4) and the rest is not a type error on either side: the client happily
uploads something the server then rejects, and the owner sees a bare ``400 Bad Request``
from ``validate_attachment_bytes`` with no idea why. That is exactly the shape of the
2026-09-17 desktop attachment report. Drift between (2) and (3) is worse -- a type in
the signature table but not the cap table makes ``validate_size_for_type``'s bare dict
lookup raise ``KeyError``.

CLAUDE.md already names this hazard class ("Lockstep constants -- NEVER move one alone").
This is that guard for attachments. Mirrors ``test_hermes_backend_owned_files_contract.py``:
read the other language's literal directly and compare, so a drift fails HERE instead of
shipping green on both sides.
"""

from __future__ import annotations

import json
import os
import re
import typing
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import messaging  # noqa: E402

# apps/backend/tests/unit/contracts/<this file> -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SHARED_TYPES = _REPO_ROOT / "packages" / "shared" / "src" / "messaging" / "types.ts"


def _shared_source() -> str:
    assert _SHARED_TYPES.is_file(), f"shared messaging types not found at {_SHARED_TYPES}"
    return _SHARED_TYPES.read_text(encoding="utf-8")


def _shared_content_types() -> list[str]:
    """The `MESSAGE_ATTACHMENT_CONTENT_TYPES` string-literal array, in declaration order."""
    source = _shared_source()
    match = re.search(r"export const MESSAGE_ATTACHMENT_CONTENT_TYPES = \[(.*?)\] as const;", source, re.DOTALL)
    assert match, "could not find MESSAGE_ATTACHMENT_CONTENT_TYPES in the shared types"
    return re.findall(r'"([^"]+)"', match.group(1))


def _shared_max_bytes() -> dict[str, int]:
    """The `MESSAGE_ATTACHMENT_MAX_BYTES` record, with its `N * 1024 * 1024` arithmetic
    evaluated -- the sizes are written as products on both sides and comparing the
    rendered expression instead of its value would miss a real change."""
    source = _shared_source()
    match = re.search(r"export const MESSAGE_ATTACHMENT_MAX_BYTES: Record<[^>]+> = \{(.*?)\n\};", source, re.DOTALL)
    assert match, "could not find MESSAGE_ATTACHMENT_MAX_BYTES in the shared types"
    return {key: _product(expression) for key, expression in re.findall(r'"([^"]+)":\s*([^,\n]+),', match.group(1))}


def _product(expression: str) -> int:
    """Evaluate an `N * M * ...` integer product. Deliberately not `eval`/`literal_eval` on
    the raw text -- only digits and `*` are accepted, so nothing else in the file can run."""
    parts = [part.strip() for part in expression.strip().split("*")]
    assert all(part.isdigit() for part in parts), f"unexpected size expression: {expression!r}"
    value = 1
    for part in parts:
        value *= int(part)
    return value


def _shared_int_const(name: str) -> int:
    match = re.search(rf"export const {name} = (\d+);", _shared_source())
    assert match, f"could not find {name} in the shared types"
    return int(match.group(1))


def _literal_content_types() -> set[str]:
    hints = typing.get_type_hints(messaging.MessageAttachment)
    return set(typing.get_args(hints["content_type"]))


def test_backend_signature_and_cap_tables_cover_the_same_types():
    """A type in one table but not the other is a ``KeyError`` in ``validate_size_for_type``
    or an uncapped upload -- never a type error."""
    assert set(messaging._ATTACHMENT_SIGNATURES) == set(messaging.ATTACHMENT_MAX_BYTES_BY_TYPE)


def test_backend_wire_literal_matches_the_backend_tables():
    assert _literal_content_types() == set(messaging.ATTACHMENT_MAX_BYTES_BY_TYPE)


def test_shared_client_allowlist_matches_the_backend():
    """The composer must not offer a type the upload route will reject with a 400, and must
    not withhold one the route would have accepted."""
    assert set(_shared_content_types()) == set(messaging.ATTACHMENT_MAX_BYTES_BY_TYPE)


def test_shared_client_size_caps_match_the_backend():
    """A client cap above the server's turns an oversized file into a 400 instead of an
    inline "too large" message; a cap below it silently blocks legitimate uploads."""
    assert _shared_max_bytes() == messaging.ATTACHMENT_MAX_BYTES_BY_TYPE


def test_shared_client_attachment_count_matches_the_backend():
    assert _shared_int_const("MESSAGE_ATTACHMENT_MAX_COUNT") == messaging.ATTACHMENT_MAX_COUNT


def test_canonical_content_type_agrees_with_the_shared_resolver():
    """Both sides normalise (filename, declared type) the same way. These are the pairs a
    macOS webview actually produces -- the desktop report's own inputs."""
    cases: list[tuple[str, str, str | None]] = [
        ("shot.png", "image/png", "image/png"),
        ("shot.PNG", "IMAGE/PNG", "image/png"),
        ("scan.pdf", "application/pdf", "application/pdf"),
        ("deck.pdf", "application/pdf; charset=binary", "application/pdf"),
        ("Archive.zip", "application/zip", "application/zip"),
        ("Archive.ZIP", "application/x-zip-compressed", "application/zip"),
        ("Archive.zip", "", "application/zip"),
        ("Archive.zip", "application/octet-stream", "application/zip"),
        # Contradictory or unsupported -- rejected, never guessed.
        ("Archive.zip", "image/png", None),
        ("notes.txt", "application/zip", None),
        ("photo.heic", "image/heic", None),
        ("scan.tiff", "image/tiff", None),
        ("logo.svg", "image/svg+xml", None),
        ("Screenshots", "", None),
    ]
    for file_name, declared, expected in cases:
        try:
            resolved: str | None = messaging.canonical_attachment_content_type(file_name, declared)
            if resolved not in messaging.ATTACHMENT_MAX_BYTES_BY_TYPE:
                resolved = None
        except ValueError:
            resolved = None
        assert resolved == expected, f"{file_name!r} declared {declared!r} -> {resolved!r}, want {expected!r}"


def test_shared_accept_attribute_offers_every_supported_type():
    """`accept` drives the macOS open panel. A type missing here is a file the native picker
    hides even though the composer and the server would both have taken it."""
    match = re.search(r"export const MESSAGE_ATTACHMENT_ACCEPT = \[(.*?)\]\.join", _shared_source(), re.DOTALL)
    assert match, "could not find MESSAGE_ATTACHMENT_ACCEPT in the shared types"
    spread_all = "...MESSAGE_ATTACHMENT_CONTENT_TYPES" in match.group(1)
    extras = set(re.findall(r'"([^"]+)"', match.group(1)))
    offered = set(_shared_content_types()) | extras if spread_all else extras
    assert set(messaging.ATTACHMENT_MAX_BYTES_BY_TYPE) <= offered
    # The legacy Windows alias and the bare extension are deliberate extras: some OSes
    # report a ZIP only by one of those. Both canonicalise to application/zip server-side.
    assert extras <= {"application/x-zip-compressed", ".zip"}, (
        f"unexpected extra accept entries: {json.dumps(sorted(extras))}"
    )
