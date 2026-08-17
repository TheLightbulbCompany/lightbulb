"""Fail if the vendored schema tag drifts from openclaw-version.json#upstream."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# apps/backend/tests/chaos/test_schema_staleness.py -> repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = Path(__file__).parent / "_schema"
_PINNED_TAG = _SCHEMA_DIR / "PINNED_TAG"
_SCHEMA_BUNDLE = _SCHEMA_DIR / "openclaw-config-schema.mjs"
_GEN_JSON_SCHEMA = _SCHEMA_DIR / "gen_json_schema.mjs"
_JSON_SCHEMA = _BACKEND_ROOT / "core" / "services" / "openclaw_config.schema.json"


@pytest.mark.chaos
def test_vendored_schema_tag_matches_pinned_upstream():
    upstream = json.loads((_REPO_ROOT / "openclaw-version.json").read_text())["upstream"]
    vendored = _PINNED_TAG.read_text().strip()
    assert vendored == upstream, (
        f"Vendored zod schema is stale: PINNED_TAG={vendored!r} but "
        f"openclaw-version.json#upstream={upstream!r}. "
        f"Re-run scripts/vendor_openclaw_schema.sh after bumping OpenClaw."
    )


@pytest.mark.chaos
def test_vendored_schema_carries_fork_runtime_scope():
    """The shared-runtime-scope fork patch adds `mcp.runtimeScope` to the STRICT
    McpConfigSchema. config.py emits mcp.runtimeScope="shared" for every image
    >= 2026.6.1, so if a re-vendor pulled a base image WITHOUT the fork patch,
    the strict schema would reject that key and crash-loop every MCP owner on
    boot. Guard that the vendored bundle actually carries the fork key — a
    staleness check on the tag alone can't see whether the patch was present in
    the source it was vendored from.
    """
    if not _SCHEMA_BUNDLE.exists():
        pytest.skip("vendored schema bundle absent (run scripts/vendor_openclaw_schema.sh)")
    bundle = _SCHEMA_BUNDLE.read_text()
    # Structural match (not a bare substring): require runtimeScope to be defined
    # as an enum carrying the "shared" value, so an incidental "runtimeScope"
    # elsewhere in the ~19k-line bundle (a comment, an unrelated schema) can't
    # satisfy the guard. esbuild renames zod's `z` import, hence the \w+ namespace.
    assert re.search(r'runtimeScope:\s*\w+\.enum\([^)]*"shared"', bundle), (
        "Vendored zod schema is missing the mcp.runtimeScope enum — the "
        "shared-runtime-scope fork patch is absent from the base image the schema "
        "was vendored from. config.py emits mcp.runtimeScope='shared', which a "
        "strict schema without this key rejects, crash-looping every MCP owner. "
        "Re-vendor from a fork tag that includes the shared-runtime-scope patch."
    )


@pytest.mark.chaos
def test_json_schema_artifact_matches_vendored_zod_schema():
    """The JSON Schema the BACKEND enforces must be derived from the CURRENT zod
    bundle.

    The backend image is Python-only, so it validates merged openclaw.json
    against core/services/openclaw_config.schema.json rather than against the
    zod bundle directly. Those two can silently diverge: a re-vendor that
    refreshes the bundle but skips `node gen_json_schema.mjs` leaves the write
    gate enforcing a schema from the PREVIOUS image — it would reject keys the
    new image accepts and accept keys it rejects. Regenerating and diffing here
    is the only thing tying them together.
    """
    if not _SCHEMA_BUNDLE.exists():
        pytest.skip("vendored schema bundle absent (run scripts/vendor_openclaw_schema.sh)")
    if shutil.which("node") is None:
        pytest.skip("node not available")

    proc = subprocess.run(
        ["node", str(_GEN_JSON_SCHEMA)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"gen_json_schema.mjs failed:\n{proc.stderr}"
    assert proc.stdout == _JSON_SCHEMA.read_text(), (
        "core/services/openclaw_config.schema.json is out of sync with the vendored "
        "zod bundle. Re-run:\n"
        "  node apps/backend/tests/chaos/_schema/gen_json_schema.mjs "
        "> apps/backend/core/services/openclaw_config.schema.json\n"
        "(scripts/vendor_openclaw_schema.sh does this for you.)"
    )
