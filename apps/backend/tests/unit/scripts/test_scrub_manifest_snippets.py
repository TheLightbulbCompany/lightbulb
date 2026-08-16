"""Tests for the pure transforms of scrub_manifest_snippets.

The script rewrites already-published manifests in place. Two properties are
load-bearing: it drops ONLY ``safety_flags[].snippet`` (everything else in a
published manifest must survive byte-for-byte), and it preserves the shape
``manifest_json`` already has on the row — a JSON string on draft rows, a
DynamoDB Map everywhere else. Flipping the shape would break the readers.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from scripts.scrub_manifest_snippets import scrub_manifest_bytes, scrub_row_manifest  # noqa: E402

_LEAKY = {
    "name": "Demo",
    "file_count": 7,
    "safety_flags": [
        {"pattern": "aws-secret-key", "severity": "high", "file": "M.md", "line": 4, "snippet": "AKIAIOSFODNN7X"}
    ],
}
_CLEAN_FLAG = {"pattern": "aws-secret-key", "severity": "high", "file": "M.md", "line": 4}


def test_scrub_row_manifest_map_shape():
    new, changed = scrub_row_manifest({"manifest_json": _LEAKY})
    assert changed is True
    assert new == {"name": "Demo", "file_count": 7, "safety_flags": [_CLEAN_FLAG]}


def test_scrub_row_manifest_string_shape_stays_a_string():
    new, changed = scrub_row_manifest({"manifest_json": json.dumps(_LEAKY)})
    assert changed is True
    assert isinstance(new, str)
    assert json.loads(new)["safety_flags"] == [_CLEAN_FLAG]


def test_scrub_row_manifest_is_idempotent():
    """A re-run must be a no-op — the script is re-runnable until deleted."""
    once, _ = scrub_row_manifest({"manifest_json": _LEAKY})
    assert scrub_row_manifest({"manifest_json": once}) == (once, False)


def test_scrub_row_manifest_leaves_clean_and_unparseable_rows_alone():
    assert scrub_row_manifest({}) == (None, False)
    assert scrub_row_manifest({"manifest_json": {"name": "Demo"}}) == ({"name": "Demo"}, False)
    assert scrub_row_manifest({"manifest_json": "{not json"}) == ("{not json", False)


def test_scrub_manifest_bytes_matches_the_upload_encoding():
    """``_upload_artifact_to_s3`` writes ``json.dumps(..., sort_keys=True)``;
    matching it keeps a re-run byte-identical."""
    out = scrub_manifest_bytes(json.dumps(_LEAKY).encode("utf-8"))
    assert out == json.dumps({"name": "Demo", "file_count": 7, "safety_flags": [_CLEAN_FLAG]}, sort_keys=True).encode(
        "utf-8"
    )
    assert b"AKIAIOSFODNN7X" not in out


def test_scrub_manifest_bytes_returns_none_when_nothing_to_do():
    assert scrub_manifest_bytes(b'{"name": "Demo"}') is None
    assert scrub_manifest_bytes(b"not json") is None
    assert scrub_manifest_bytes(b"\xff\xfe") is None
