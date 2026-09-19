import hashlib
import json
import stat
from pathlib import Path

import pytest

from core.services.credentials.materializers import mcp_oauth, mcporter_vault


def _patch_efs(monkeypatch, tmp_path):
    # Materializers only run inside a projection (credentials/hermes_cli.py), so the root is
    # the ContextVar's, not a settings path. tmp_path/"org_1" keeps every path assertion below.
    # mcporter_vault is the second writer of the same connect and resolves its own root.
    for mod in (mcp_oauth, mcporter_vault):
        monkeypatch.setattr(mod, "materializer_root", lambda: tmp_path / "org_1")


@pytest.fixture
def cred():
    # What oauth_dance.exchange surfaces for a DCR/public client: the full
    # registered-client JSON plus the token fields.
    return {
        "access_token": "at_tok",
        "refresh_token": "rt_tok",
        "expires_in": 3600,
        "token_expiry": "2030-01-01T00:00:00+00:00",
        "client_id": "dcr_cid",
        "client_secret": "",
        "client_information": {
            "client_id": "dcr_cid",
            "redirect_uris": ["https://app.isol8.co/api/v1/oauth/attio/callback"],
            "token_endpoint_auth_method": "none",
        },
    }


def _expected_store_path(tmp_path, owner_id, server_name, server_url):
    h = hashlib.sha256()
    h.update(server_name.encode())
    h.update(b"\0")
    h.update(server_url.encode())
    key16 = h.hexdigest()[:16]
    return Path(tmp_path) / owner_id / "mcp-oauth" / f"{server_name}-{key16}.json"


def test_write_seeds_sdk_store(monkeypatch, tmp_path, cred):
    # Tests run non-root, so base.atomic_write skips the chown branch naturally.
    _patch_efs(monkeypatch, tmp_path)

    identity = mcp_oauth.write("org_1", "attio", cred)

    # Identity is the server display key (mirrors gog_cli returning the email).
    assert identity == "attio"

    # File lands at OpenClaw's exact hashed path: <root>/mcp-oauth/attio-<key16>.json.
    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    assert expected.is_file()

    # The hash matches sha256(name + \0 + url)[:16].
    assert expected.name == "attio-" + hashlib.sha256(b"attio\x00https://mcp.attio.com/mcp").hexdigest()[:16] + ".json"

    # Mode is 0600.
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600

    # Store carries EXACTLY {clientInformation, tokens} — the bit-for-bit CLI output.
    store = json.loads(expected.read_text())
    assert set(store) == {"clientInformation", "tokens"}
    assert store["clientInformation"] == cred["client_information"]
    assert store["tokens"] == {
        "access_token": "at_tok",
        "token_type": "Bearer",
        "refresh_token": "rt_tok",
        "expires_in": 3600,
        "scope": "openid offline_access mcp",
    }

    # The materializer publishes no server block: the reconcile pass rebuilds the whole
    # mcp_servers map (including auth: "oauth") from the registry + connected set.


def test_write_omits_empty_token_fields(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)

    # No refresh token and no expiry — those keys must be omitted, not written null.
    cred = {
        "access_token": "at_only",
        "refresh_token": "",
        "expires_in": None,
        "client_information": {"client_id": "dcr_cid"},
    }
    mcp_oauth.write("org_1", "attio", cred)

    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    tokens = json.loads(expected.read_text())["tokens"]
    assert "refresh_token" not in tokens
    assert "expires_in" not in tokens
    assert tokens == {"access_token": "at_only", "token_type": "Bearer", "scope": "openid offline_access mcp"}


def test_remove_deletes_store_file(monkeypatch, tmp_path, cred):
    _patch_efs(monkeypatch, tmp_path)

    mcp_oauth.write("org_1", "attio", cred)
    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    assert expected.is_file()

    mcp_oauth.remove("org_1", "attio", {})

    assert not expected.exists()


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path):
    _patch_efs(monkeypatch, tmp_path)
    # No store file exists — remove must not raise (force / missing_ok).
    mcp_oauth.remove("org_1", "attio", {})


def test_safe_server_name_strips_disallowed_chars():
    # Author keys already match [a-z0-9_-]; the sanitizer reproduces OpenClaw's
    # identity behavior on them and drops anything outside the charset.
    assert mcp_oauth._safe_server_name("attio") == "attio"
    assert mcp_oauth._safe_server_name("My Server!") == "myserver"
