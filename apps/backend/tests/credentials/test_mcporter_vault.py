"""Unit tests for the mcporter_vault materializer helper.

The vault-key hash MUST match mcporter's own ``JSON.stringify``-based key or a
``mcporter call`` won't find the entry we seed; the shared vault/config must MERGE
per server, not overwrite a sibling provider's entry.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.services.credentials.materializers import mcporter_vault

ATTIO_URL = "https://mcp.attio.com/mcp"
NOTION_URL = "https://mcp.notion.com/mcp"


@pytest.fixture
def efs(tmp_path, monkeypatch):
    # Redirect the per-owner EFS root the helper writes under to a temp dir.
    monkeypatch.setattr(mcporter_vault, "owner_efs_root", lambda owner_id: tmp_path / owner_id)
    return tmp_path


def _cred(access="at1", refresh="rt1", client_id="cid1"):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 3600,
        "client_information": {"client_id": client_id, "token_endpoint_auth_method": "none"},
    }


def _vault(efs, owner="owner1"):
    return json.loads((efs / owner / "mcporter" / "credentials.json").read_text())


def _config(efs, owner="owner1"):
    return json.loads((efs / owner / "mcporter" / "mcporter.json").read_text())


def _expected_key(name: str, url: str) -> str:
    payload = json.dumps({"name": name, "url": url, "command": None}, separators=(",", ":"))
    return f"{name}|{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def test_seed_writes_vault_entry_and_config(efs):
    mcporter_vault.seed("owner1", "attio", ATTIO_URL, _cred(), ["openid", "mcp"])

    vault = _vault(efs)
    assert vault["version"] == 1
    entry = vault["entries"][_expected_key("attio", ATTIO_URL)]
    assert entry["serverName"] == "attio"
    assert entry["serverUrl"] == ATTIO_URL
    assert entry["tokens"] == {
        "access_token": "at1",
        "token_type": "Bearer",
        "refresh_token": "rt1",
        "expires_in": 3600,
        "scope": "openid mcp",
    }
    assert entry["clientInfo"]["client_id"] == "cid1"
    assert "updatedAt" in entry

    assert _config(efs)["mcpServers"]["attio"] == {"url": ATTIO_URL, "auth": "oauth"}


def test_seed_merges_a_second_server(efs):
    mcporter_vault.seed("owner1", "attio", ATTIO_URL, _cred(client_id="a"), [])
    mcporter_vault.seed("owner1", "notion", NOTION_URL, _cred(client_id="n"), [])

    vault = _vault(efs)
    assert set(vault["entries"]) == {_expected_key("attio", ATTIO_URL), _expected_key("notion", NOTION_URL)}
    assert set(_config(efs)["mcpServers"]) == {"attio", "notion"}


def test_unseed_removes_only_the_target(efs):
    mcporter_vault.seed("owner1", "attio", ATTIO_URL, _cred(), [])
    mcporter_vault.seed("owner1", "notion", NOTION_URL, _cred(), [])

    mcporter_vault.unseed("owner1", "attio", ATTIO_URL)

    assert list(_config(efs)["mcpServers"]) == ["notion"]
    assert all(e["serverName"] == "notion" for e in _vault(efs)["entries"].values())


def test_unseed_missing_files_is_noop(efs):
    mcporter_vault.unseed("owner1", "attio", ATTIO_URL)  # no files yet — must not raise


def test_missing_refresh_token_is_dropped(efs):
    cred = _cred()
    del cred["refresh_token"]
    mcporter_vault.seed("owner1", "attio", ATTIO_URL, cred, [])

    entry = next(iter(_vault(efs)["entries"].values()))
    assert "refresh_token" not in entry["tokens"]
