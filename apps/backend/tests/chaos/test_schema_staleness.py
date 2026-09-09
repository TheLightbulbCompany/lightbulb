"""Guards on every registered OpenClaw config schema artifact."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core.services.config_schema import _SCHEMA_PATHS


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_DIR = Path(__file__).parent / "_schema"
_GEN_JSON_SCHEMA = _SCHEMA_DIR / "gen_json_schema.mjs"
_LEGACY_ID = "2026.8.1-isol8.3"
_LEGACY_BUNDLE = _SCHEMA_DIR / "openclaw-config-schema.mjs"
_LEGACY_HASHES = {
    "bundle": "6e194e95823db3a58af8ca6708291ca1a9cf45149806de60851f006f20c71fc6",
    "json": "ce18b8954fdc14687c83925bc5157b70ef40e3a9fd28fd049e70cbc5bd0d31a1",
    "tag": "f11970c87b9a1ef73d8a0577ff24103aeea637993ee2f3655f396474fded58dd",
}


def _bundle_path(schema_id: str) -> Path:
    if schema_id == _LEGACY_ID:
        return _LEGACY_BUNDLE
    return _SCHEMA_DIR / "releases" / schema_id / "openclaw-config-schema.mjs"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.chaos
@pytest.mark.parametrize("schema_id", tuple(_SCHEMA_PATHS))
def test_registered_schema_carries_fork_runtime_scope(schema_id: str):
    """Every selectable schema must accept the shared MCP runtime scope."""
    bundle = _bundle_path(schema_id).read_text()
    assert re.search(r'runtimeScope:\s*\w+\.enum\([^)]*"shared"', bundle), (
        f"Registered schema {schema_id} is missing the mcp.runtimeScope enum; re-vendor from the exact fork release."
    )


@pytest.mark.chaos
@pytest.mark.parametrize("schema_id,json_schema", tuple(_SCHEMA_PATHS.items()))
def test_registered_json_schema_matches_its_zod_bundle(schema_id: str, json_schema: Path):
    """Each Python artifact must regenerate from its own registered bundle."""
    assert shutil.which("node") is not None, "Backend CI must provide Node20 for schema proof"
    bundle = _bundle_path(schema_id)
    assert bundle.is_file(), f"registered schema bundle is missing: {bundle}"
    assert json_schema.is_file(), f"registered JSON schema is missing: {json_schema}"

    proc = subprocess.run(
        ["node", str(_GEN_JSON_SCHEMA), str(bundle)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"gen_json_schema.mjs failed for {schema_id}:\n{proc.stderr}"
    assert proc.stdout == json_schema.read_text(), (
        f"{json_schema} is out of sync with {bundle}; re-vendor schema {schema_id}"
    )


def test_legacy_fleet_artifacts_remain_byte_identical():
    """A candidate is additive; the active 8.1 fleet evidence is immutable."""
    assert _sha256(_LEGACY_BUNDLE) == _LEGACY_HASHES["bundle"]
    assert _sha256(_SCHEMA_PATHS[_LEGACY_ID]) == _LEGACY_HASHES["json"]
    assert _sha256(_SCHEMA_DIR / "PINNED_TAG") == _LEGACY_HASHES["tag"]


@pytest.mark.parametrize("schema_id", tuple(key for key in _SCHEMA_PATHS if key != _LEGACY_ID))
def test_candidate_artifacts_match_their_provenance(schema_id: str):
    release = _SCHEMA_DIR / "releases" / schema_id
    provenance_path = release / "PROVENANCE.json"
    assert provenance_path.is_file(), f"registered candidate provenance is missing: {provenance_path}"
    provenance = json.loads(provenance_path.read_text())
    assert provenance["schema_id"] == schema_id
    assert provenance["tag"] == f"v{schema_id}"
    assert re.fullmatch(r"[0-9a-f]{40}", provenance["commit"])
    assert provenance["bundle_sha256"] == _sha256(_bundle_path(schema_id))
    assert provenance["json_sha256"] == _sha256(_SCHEMA_PATHS[schema_id])
