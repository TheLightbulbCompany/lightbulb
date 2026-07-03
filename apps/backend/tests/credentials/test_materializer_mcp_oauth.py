import hashlib
import json
import stat
from pathlib import Path

import pytest

from core.services.credentials.materializers import mcp_oauth


def _patch_efs(monkeypatch, tmp_path):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))


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


@pytest.fixture
def captured_config(monkeypatch):
    """Record set/delete config-path calls instead of touching a real openclaw.json.

    The materializer drives the async config_patcher via asyncio.run inside its
    worker-thread body, so patch the async functions with async stand-ins."""
    calls = {"set": [], "delete": []}

    async def _fake_set(owner_id, path, value):
        calls["set"].append((owner_id, path, value))

    async def _fake_delete(owner_id, path):
        calls["delete"].append((owner_id, path))

    monkeypatch.setattr(mcp_oauth.config_patcher, "set_openclaw_config_path", _fake_set)
    monkeypatch.setattr(mcp_oauth.config_patcher, "delete_openclaw_config_path", _fake_delete)
    return calls


def _expected_store_path(tmp_path, owner_id, server_name, server_url):
    h = hashlib.sha256()
    h.update(server_name.encode())
    h.update(b"\0")
    h.update(server_url.encode())
    key16 = h.hexdigest()[:16]
    return Path(tmp_path) / owner_id / "mcp-oauth" / f"{server_name}-{key16}.json"


def test_write_seeds_sdk_store_and_publishes_server_block(monkeypatch, tmp_path, cred, captured_config):
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

    # The live mcp.servers.attio streamable-http block was published WITH auth:
    # "oauth" — the field that arms OpenClaw's MCP SDK OAuthClientProvider so it
    # reads the seeded token store (without it the server is treated as plain and 401s).
    assert captured_config["set"] == [
        (
            "org_1",
            ["mcp", "servers", "attio"],
            {
                "transport": "streamable-http",
                "url": "https://mcp.attio.com/mcp",
                "auth": "oauth",
                "requestTimeoutMs": 60000,
            },
        )
    ]
    assert captured_config["delete"] == []


def test_write_omits_empty_token_fields(monkeypatch, tmp_path, captured_config):
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


def test_remove_deletes_store_file_and_server_block(monkeypatch, tmp_path, cred, captured_config):
    _patch_efs(monkeypatch, tmp_path)

    mcp_oauth.write("org_1", "attio", cred)
    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    assert expected.is_file()

    mcp_oauth.remove("org_1", "attio", {})

    # Store file is gone and the server block was removed.
    assert not expected.exists()
    assert captured_config["delete"] == [("org_1", ["mcp", "servers", "attio"])]


def test_remove_is_idempotent_when_missing(monkeypatch, tmp_path, captured_config):
    _patch_efs(monkeypatch, tmp_path)
    # No store file exists — remove must not raise (force / missing_ok).
    mcp_oauth.remove("org_1", "attio", {})
    assert captured_config["delete"] == [("org_1", ["mcp", "servers", "attio"])]


def test_safe_server_name_strips_disallowed_chars():
    # Author keys already match [a-z0-9_-]; the sanitizer reproduces OpenClaw's
    # identity behavior on them and drops anything outside the charset.
    assert mcp_oauth._safe_server_name("attio") == "attio"
    assert mcp_oauth._safe_server_name("My Server!") == "myserver"


@pytest.mark.asyncio
async def test_remove_succeeds_when_called_on_running_loop(monkeypatch, tmp_path, cred, captured_config):
    # The materializer's config-patcher invocation is robust to a running loop: when
    # remove() is called directly from a coroutine (a caller that forgot the
    # run_in_thread offload), _run_sync drives the coroutine on a fresh loop in a
    # worker thread instead of letting asyncio.run raise. So a direct synchronous
    # call from this live event loop (pytest-asyncio) removes the store file + the
    # server block WITHOUT raising RuntimeError.
    _patch_efs(monkeypatch, tmp_path)
    # write() is robust the same way, so it can run directly on the loop too.
    mcp_oauth.write("org_1", "attio", cred)
    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    assert expected.is_file()

    mcp_oauth.remove("org_1", "attio", {})

    assert not expected.exists()
    assert captured_config["delete"] == [("org_1", ["mcp", "servers", "attio"])]


@pytest.mark.asyncio
async def test_remove_via_run_in_thread_succeeds_from_async_context(monkeypatch, tmp_path, cred, captured_config):
    # The fix: offloading the remover through run_in_thread runs asyncio.run on a
    # thread with no running loop, so a disconnect from inside an async handler
    # removes the store file + server block WITHOUT raising RuntimeError.
    from core.dynamodb import run_in_thread

    _patch_efs(monkeypatch, tmp_path)
    await run_in_thread(mcp_oauth.write, "org_1", "attio", cred)
    expected = _expected_store_path(tmp_path, "org_1", "attio", "https://mcp.attio.com/mcp")
    assert expected.is_file()

    await run_in_thread(mcp_oauth.remove, "org_1", "attio", {})

    assert not expected.exists()
    assert captured_config["delete"] == [("org_1", ["mcp", "servers", "attio"])]
