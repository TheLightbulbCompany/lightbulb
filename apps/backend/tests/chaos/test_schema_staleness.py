"""Fail if the vendored schema tag drifts from openclaw-version.json#upstream."""

import json
from pathlib import Path

import pytest

# apps/backend/tests/chaos/test_schema_staleness.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PINNED_TAG = Path(__file__).parent / "_schema" / "PINNED_TAG"


@pytest.mark.chaos
def test_vendored_schema_tag_matches_pinned_upstream():
    upstream = json.loads((_REPO_ROOT / "openclaw-version.json").read_text())["upstream"]
    vendored = _PINNED_TAG.read_text().strip()
    assert vendored == upstream, (
        f"Vendored zod schema is stale: PINNED_TAG={vendored!r} but "
        f"openclaw-version.json#upstream={upstream!r}. "
        f"Re-run scripts/vendor_openclaw_schema.sh after bumping OpenClaw."
    )
