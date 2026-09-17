import yaml

from core.services import hermes_profiles


def _setup(monkeypatch, *, stored_config, connected, dotenv_by_agent=None):
    puts = []
    monkeypatch.setattr(hermes_profiles, "read_profile_config", lambda o, a: dict(stored_config))
    monkeypatch.setattr(hermes_profiles, "_connected_servers", lambda owner_id: connected)
    monkeypatch.setattr(hermes_profiles, "_write_mcp_token_files", lambda o, a, servers: None)
    monkeypatch.setattr(hermes_profiles, "_read_dotenv", lambda o, a: (dotenv_by_agent or {}).get(a, ""))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((key, body)))
    return puts


def _config_writes(puts, agent_id):
    return [body for key, body in puts if key == f"org_a/hermes/{agent_id}/config.yaml"]


def test_refresh_rewrites_every_profile_and_bumps_each_revision(monkeypatch):
    puts = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}},
        connected={"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth", "requestTimeoutMs": 60000}},
    )
    hermes_profiles.refresh_integrations("org_a", ["a1", "a2"])
    for agent_id in ("a1", "a2"):
        (config_yaml,) = _config_writes(puts, agent_id)
        assert yaml.safe_load(config_yaml)["mcp_servers"]["stripe"] == {
            "url": "https://mcp.stripe.com",
            "auth": "oauth",
            "timeout": 60,
        }
        keys = [key for key, _ in puts if key.startswith(f"org_a/hermes/{agent_id}/")]
        assert keys[-1] == f"org_a/hermes/{agent_id}/.revision"  # written LAST for every agent


def test_refresh_replaces_the_whole_mcp_block_so_a_disconnect_removes_a_server(monkeypatch):
    puts = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {"posthog": {"url": "https://old"}}},
        connected={},
    )
    hermes_profiles.refresh_integrations("org_a", ["a1"])
    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["mcp_servers"] == {}


def test_refresh_preserves_every_other_config_key(monkeypatch):
    """M4-R11: refresh_integrations rewrites ONLY mcp_servers — everything else in the profile's
    config.yaml (model pins, approvals, aux tasks...) must survive untouched."""
    puts = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}, "model": {"default": "arn:son", "provider": "bedrock"}},
        connected={},
    )
    hermes_profiles.refresh_integrations("org_a", ["a1"])
    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["model"] == {"default": "arn:son", "provider": "bedrock"}


def test_refresh_moves_mcp_secrets_to_env_and_drops_a_disconnected_ones_old_secret(monkeypatch):
    """I1's ${MCP_<SERVER>_<VAR>} refs need the matching .env value kept in sync on every
    refresh — a stale secret for a provider that just disconnected must not linger."""
    puts = _setup(
        monkeypatch,
        stored_config={"mcp_servers": {}},
        connected={"acme": {"command": "npx", "env": {"ACME_TOKEN": "sk_new"}}},
        dotenv_by_agent={"a1": "PAPERCLIP_API_KEY=pcp_x\nMCP_OLD_TOKEN=stale\n"},
    )
    hermes_profiles.refresh_integrations("org_a", ["a1"])
    env_writes = [body for key, body in puts if key == "org_a/hermes/a1/.env"]
    assert len(env_writes) == 1
    env_text = env_writes[0]
    assert "PAPERCLIP_API_KEY=pcp_x" in env_text
    assert "MCP_OLD_TOKEN" not in env_text
    assert "MCP_ACME_ACME_TOKEN=sk_new" in env_text

    (config_yaml,) = _config_writes(puts, "a1")
    assert yaml.safe_load(config_yaml)["mcp_servers"]["acme"]["env"] == {"ACME_TOKEN": "${MCP_ACME_ACME_TOKEN}"}
    assert "sk_new" not in config_yaml


def test_rewrite_mcp_env_lines_keeps_non_mcp_lines_and_replaces_mcp_ones():
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
    monkeypatch.setattr(
        hermes_profiles,
        "_integration_credential",
        lambda owner_id, name: {"access_token": "at", "refresh_token": "rt"},
    )
    hermes_profiles._write_mcp_token_files(
        "org_a",
        "a1",
        {"stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"}, "qbo": {"command": "npx"}},
    )
    assert set(written) == {"stripe"}
    assert written["stripe"]["access_token"] == "at"


def test_write_mcp_token_files_is_empty_for_no_servers():
    # Must not raise or call _integration_credential when there is nothing to seed.
    hermes_profiles._write_mcp_token_files("org_a", "a1", {})


def test_put_token_file_writes_under_mcp_tokens(monkeypatch):
    puts = []
    monkeypatch.setattr(hermes_profiles, "_s3_put", lambda bucket, key, body: puts.append((bucket, key, body)))
    monkeypatch.setattr(hermes_profiles.settings, "OWNER_STATE_BUCKET", "b")
    hermes_profiles._put_token_file("org_a", "a1", "stripe", {"access_token": "at"})
    bucket, key, body = puts[0]
    assert bucket == "b"
    assert key == "org_a/hermes/a1/mcp-tokens/stripe.json"
    assert '"access_token": "at"' in body
