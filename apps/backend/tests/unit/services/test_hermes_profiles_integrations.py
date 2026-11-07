from unittest.mock import AsyncMock

import pytest
import yaml

import core.repositories.paperclip_repo as paperclip_repo_module
import core.services.credentials.integration_secret_store as secret_store_module
import core.services.credentials.mcp_server as mcp_server_module
from core.services import hermes_profiles


def _setup(monkeypatch, *, stored_config, connected, dotenv_by_agent=None, existing_tokens=None, credential=None):
    """Routes _s3_get/_s3_put/_s3_delete by key suffix -- the real seam refresh_integrations now
    calls directly (I1: no more read_profile_config/_read_dotenv/_connected_servers wrappers)."""
    puts: list[tuple[str, str]] = []
    deletes: list[str] = []
    existing_tokens = existing_tokens or set()  # {"a1:stripe", ...} -- already-seeded token files

    async def fake_connected(owner_id, *, pending=None):
        return connected

    async def fake_cred(owner_id, provider):
        return credential or {"access_token": "at", "refresh_token": "rt"}

    def fake_s3_get(bucket, key):
        if key.endswith("config.yaml"):
            return yaml.safe_dump(stored_config)
        if key.endswith(".env"):
            agent_id = key.split("/")[2]
            return (dotenv_by_agent or {}).get(agent_id, "")
        if "/mcp-tokens/" in key:
            agent_id = key.split("/")[2]
            name = key.rsplit("/", 1)[1].removesuffix(".json")
            return "{}" if f"{agent_id}:{name}" in existing_tokens else None
        return None

    monkeypatch.setattr(mcp_server_module, "build_connected_integration_servers", fake_connected)
    monkeypatch.setattr(secret_store_module, "get", fake_cred)
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    monkeypatch.setattr(hermes_profiles, "_s3_get", fake_s3_get)
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((key, body)))
    monkeypatch.setattr(hermes_profiles, "_s3_delete", lambda bucket, key: deletes.append(key))
    return puts, deletes


@pytest.fixture(autouse=True)
def _front_desk(monkeypatch):
    """The workspace's Front desk agent (refresh_integrations reads it once per refresh)."""
    monkeypatch.setattr(paperclip_repo_module, "customer_ops_agent_id", AsyncMock(return_value="fd"))


def _config_writes(puts, agent_id):
    return [body for key, body in puts if key == f"org_a/hermes/{agent_id}/config.yaml"]


async def test_refresh_rewrites_every_profile_and_bumps_each_revision(monkeypatch):
    puts, _ = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}},
        connected={"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth", "requestTimeoutMs": 60000}},
    )
    await hermes_profiles.refresh_integrations("org_a", ["a1", "a2"])
    for agent_id in ("a1", "a2"):
        (config_yaml,) = _config_writes(puts, agent_id)
        assert yaml.safe_load(config_yaml)["mcp_servers"]["stripe"] == {
            "url": "https://mcp.stripe.com",
            "auth": "oauth",
            "timeout": 60,
        }
        keys = [key for key, _ in puts if key.startswith(f"org_a/hermes/{agent_id}/")]
        assert keys[-1] == f"org_a/hermes/{agent_id}/.revision"  # written LAST for every agent


async def test_refresh_replaces_the_whole_mcp_block_so_a_disconnect_removes_a_server(monkeypatch):
    puts, _ = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {"posthog": {"url": "https://old"}}},
        connected={},
    )
    await hermes_profiles.refresh_integrations("org_a", ["a1"])
    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["mcp_servers"] == {}


async def test_refresh_preserves_every_other_config_key(monkeypatch):
    """M4-R11: refresh_integrations rewrites ONLY mcp_servers — everything else in the profile's
    config.yaml (model pins, approvals, aux tasks...) must survive untouched."""
    puts, _ = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}, "model": {"default": "arn:son", "provider": "bedrock"}},
        connected={},
    )
    await hermes_profiles.refresh_integrations("org_a", ["a1"])
    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["model"] == {"default": "arn:son", "provider": "bedrock"}


async def test_refresh_moves_mcp_secrets_to_env_and_drops_a_disconnected_ones_old_secret(monkeypatch):
    """I1's ${MCP_<SERVER>_<VAR>} refs need the matching .env value kept in sync on every
    refresh — a stale secret for a provider that just disconnected must not linger."""
    puts, _ = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}},
        connected={"acme": {"command": "npx", "env": {"ACME_TOKEN": "sk_new"}}},
        dotenv_by_agent={"a1": "PAPERCLIP_API_KEY=pcp_x\nMCP_OLD_TOKEN=stale\n"},
    )
    await hermes_profiles.refresh_integrations("org_a", ["a1"])
    env_writes = [body for key, body in puts if key == "org_a/hermes/a1/.env"]
    assert len(env_writes) == 1
    env_text = env_writes[0]
    assert "PAPERCLIP_API_KEY=pcp_x" in env_text
    assert "MCP_OLD_TOKEN" not in env_text
    assert "MCP_ACME_ACME_TOKEN=sk_new" in env_text

    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["mcp_servers"]["acme"]["env"] == {
        "ACME_TOKEN": "${MCP_ACME_ACME_TOKEN}",
        "HOME": "/opt/data/profiles/a1/home",
    }
    assert "sk_new" not in config_yaml


async def test_refresh_raises_when_the_profile_has_no_config_yaml_yet(monkeypatch):
    """C4: a reconcile onto a profile that was never provisioned is a defect, not something to
    merge onto {} and upload."""
    import pytest

    puts, _ = _setup(monkeypatch, stored_config={"mcp_servers": {}}, connected={})
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)
    with pytest.raises(FileNotFoundError):
        await hermes_profiles.refresh_integrations("org_a", ["a1"])
    assert puts == []


async def test_refresh_raises_when_the_profile_has_no_env_yet(monkeypatch):
    """C4: same defect class for a missing .env -- never write one holding only MCP lines."""
    import pytest

    def fake_s3_get(bucket, key):
        if key.endswith("config.yaml"):
            return yaml.safe_dump({"mcp_servers": {}})
        return None  # .env absent

    puts, _ = _setup(monkeypatch, stored_config={"mcp_servers": {}}, connected={})
    monkeypatch.setattr(hermes_profiles, "_s3_get", fake_s3_get)
    with pytest.raises(FileNotFoundError):
        await hermes_profiles.refresh_integrations("org_a", ["a1"])
    assert puts == []


async def test_refresh_runs_from_inside_a_running_event_loop(monkeypatch):
    """I1 (M4-R46/M4-R11): the old asyncio.run()-based sync wrappers raised RuntimeError the
    moment a caller awaited this from inside a running loop -- that IS the calling context
    (reconcile_integrations is itself async)."""
    _setup(monkeypatch, stored_config={"mcp_servers": {}}, connected={})
    await hermes_profiles.refresh_integrations("org_a", ["a1"])  # must not raise RuntimeError


async def test_refresh_never_calls_boto3_on_the_event_loop_thread(monkeypatch):
    """M4-R49 (fix round 2): refresh_integrations is awaited directly from routers/services now
    (routers/integrations.py, catalog_service.py) -- every sync boto3 call inside its per-agent
    block must run off the loop (the 2026-09-12 prod-freeze class), not just the async producers
    it already awaits directly."""
    import threading

    loop_thread_id = threading.get_ident()
    seen_thread_ids = []

    def fake_s3_get(bucket, key):
        seen_thread_ids.append(threading.get_ident())
        if key.endswith("config.yaml"):
            return yaml.safe_dump({"mcp_servers": {}})
        return ""  # .env absent-but-empty is fine here -- only the thread id matters

    def fake_s3_put(bucket, key, body):
        seen_thread_ids.append(threading.get_ident())

    _setup(monkeypatch, stored_config={"mcp_servers": {}}, connected={})
    monkeypatch.setattr(hermes_profiles, "_s3_get", fake_s3_get)
    monkeypatch.setattr(hermes_profiles, "_s3_put", fake_s3_put)
    await hermes_profiles.refresh_integrations("org_a", ["a1"])
    assert seen_thread_ids  # the stub was actually exercised
    assert loop_thread_id not in seen_thread_ids


async def test_refresh_reads_one_credential_per_server_not_per_agent(monkeypatch):
    """Minor 8: hoisted out of the per-agent loop -- one Secrets Manager read per OAuth server
    per reconcile, regardless of how many agents share the profile."""
    reads = []

    async def fake_cred(owner_id, provider):
        reads.append(provider)
        return {"access_token": "at"}

    _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}},
        connected={"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"}},
    )
    monkeypatch.setattr(secret_store_module, "get", fake_cred)
    await hermes_profiles.refresh_integrations("org_a", ["a1", "a2", "a3"])
    assert reads == ["stripe"]


async def test_rewrite_mcp_env_lines_keeps_non_mcp_lines_and_replaces_mcp_ones():
    out = hermes_profiles._rewrite_mcp_env_lines(
        "PAPERCLIP_API_KEY=pcp_x\nMCP_STRIPE_TOKEN=old\nOPENAI_API_KEY=sk-x\n",
        {"MCP_QBO_TOKEN": "new"},
    )
    lines = out.splitlines()
    assert "PAPERCLIP_API_KEY=pcp_x" in lines
    assert "OPENAI_API_KEY=sk-x" in lines
    assert "MCP_QBO_TOKEN=new" in lines
    assert not any(line.startswith("MCP_STRIPE_TOKEN") for line in lines)


def test_rewrite_mcp_env_lines_handles_an_empty_starting_dotenv():
    out = hermes_profiles._rewrite_mcp_env_lines("", {"MCP_QBO_TOKEN": "new"})
    assert out == "MCP_QBO_TOKEN=new"


def test_oauth_remote_servers_get_a_token_file_and_key_based_ones_do_not(monkeypatch):
    written = {}
    monkeypatch.setattr(hermes_profiles, "_put_token_file", lambda o, a, name, blob: written.setdefault(name, blob))
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: None)  # nothing seeded yet
    hermes_profiles._write_mcp_token_files(
        "org_a",
        "a1",
        {"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"}, "qbo": {"command": "npx"}},
        {"stripe": {"access_token": "at", "refresh_token": "rt"}},
    )
    assert set(written) == {"stripe"}
    assert written["stripe"]["access_token"] == "at"


def test_write_mcp_token_files_is_empty_for_no_servers():
    # Must not raise when there is nothing to seed.
    hermes_profiles._write_mcp_token_files("org_a", "a1", {}, {})


def test_a_second_reconcile_does_not_rewrite_an_already_seeded_token_file(monkeypatch):
    """M4-R47/I2: the container is the sole rotator once a token file has been seeded once --
    a live, already-rotated token must never be clobbered by a stale credential-store copy."""
    written = {}
    monkeypatch.setattr(hermes_profiles, "_put_token_file", lambda o, a, name, blob: written.setdefault(name, blob))
    monkeypatch.setattr(hermes_profiles, "_s3_get", lambda bucket, key: "{}")  # already present
    hermes_profiles._write_mcp_token_files(
        "org_a", "a1", {"stripe": {"auth": "oauth"}}, {"stripe": {"access_token": "stale"}}
    )
    assert written == {}


def test_a_disconnected_servers_token_file_is_deleted(monkeypatch):
    """M4-R47/I2: so a later reconnect seeds a fresh one instead of resurrecting the old grant."""
    deletes = []
    monkeypatch.setattr(hermes_profiles, "_s3_delete", lambda bucket, key: deletes.append(key))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles._write_mcp_token_files("org_a", "a1", {}, {}, previously_oauth=frozenset({"stripe"}))
    assert deletes == ["org_a/hermes/a1/mcp-tokens/stripe.json"]


async def test_refresh_seeds_once_deletes_on_disconnect_and_writes_a_newly_connected_server(monkeypatch):
    """End-to-end I2 test at the refresh_integrations level: stripe was already seeded (untouched),
    posthog just disconnected (its token file is deleted), acme just connected (freshly seeded)."""
    _, deletes = _setup(
        monkeypatch,
        stored_config={
            "mcp_servers": {
                "stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"},
                "posthog": {"url": "https://mcp.posthog.com", "auth": "oauth"},
            }
        },
        connected={
            "stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"},
            "acme": {"url": "https://mcp.acme.com", "auth": "oauth"},
        },
        existing_tokens={"a1:stripe"},
    )
    token_puts = []
    monkeypatch.setattr(
        hermes_profiles,
        "_put_token_file",
        lambda o, a, name, blob: token_puts.append(name),
    )
    await hermes_profiles.refresh_integrations("org_a", ["a1"])
    assert token_puts == ["acme"]  # stripe untouched (already seeded), only the new one is written
    assert deletes == ["org_a/hermes/a1/mcp-tokens/posthog.json"]


def test_put_token_file_writes_under_mcp_tokens(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((bucket, key, body)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles._put_token_file("org_a", "a1", "stripe", {"access_token": "at"})
    bucket, key, body = puts[0]
    assert bucket == "b"
    assert key == "org_a/hermes/a1/mcp-tokens/stripe.json"
    assert '"access_token": "at"' in body


async def test_a_connect_never_reaches_the_front_desk_profile(monkeypatch):
    """End-state Task 4: every connect re-asserts the owner's servers into each profile; Front desk's gets only the
    contact-safe ones (none today), and a server it already held is removed with its secret."""
    puts, _ = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {"stripe": {"url": "https://old"}}},
        connected={"stripe": {"url": "https://mcp.stripe.com", "env": {"KEY": "sk_live"}}},
        dotenv_by_agent={"fd": "MCP_STRIPE_KEY=sk_old\n", "office": ""},
    )
    await hermes_profiles.refresh_integrations("org_a", ["fd", "office"])
    assert yaml.safe_load(_config_writes(puts, "fd")[0])["mcp_servers"] == {}
    assert "sk_" not in next(body for key, body in puts if key == "org_a/hermes/fd/.env")
    assert set(yaml.safe_load(_config_writes(puts, "office")[0])["mcp_servers"]) == {"stripe"}
