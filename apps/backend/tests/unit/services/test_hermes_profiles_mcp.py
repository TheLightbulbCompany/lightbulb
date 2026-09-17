import json

from core.services.hermes_profiles import translate_mcp_servers


def test_remote_oauth_server_keeps_auth_oauth_and_snake_cases_timeouts_no_secrets_to_move():
    config, secrets = translate_mcp_servers(
        {
            "stripe": {
                "transport": "streamable-http",
                "url": "https://mcp.stripe.com",
                "auth": "oauth",
                "requestTimeoutMs": 60000,
                "connectionTimeoutMs": 15000,
            }
        }
    )
    assert config["stripe"] == {
        "url": "https://mcp.stripe.com",
        "auth": "oauth",
        "timeout": 60,
        "connect_timeout": 15,
    }
    assert secrets == {}  # this real mcp_remote shape carries no env/headers — nothing to move


def test_stdio_server_env_values_move_to_secrets_and_config_keeps_only_a_var_reference():
    """Fix round 1, I1: producer-shaped input. ``credentials.mcp_server.build_mcp_server_block``
    returns exactly ``{command, args, env: {var: <plaintext resolved credential>}, connectionTimeoutMs}``
    — ``env`` is a real, paste-key-resolved credential value (``registry.py``: "``mcp_server`` (the
    wiring rail) resolves each env var from the retained Secrets Manager secret"), never a path or
    a reference. It must never land in ``config.yaml`` verbatim — that file is also served to the
    settings UI (M4-R1) and rides S3."""
    config, secrets = translate_mcp_servers(
        {
            "acme_crm": {
                "command": "npx",
                "args": ["-y", "acme-crm-mcp"],
                "env": {"ACME_API_TOKEN": "sk_live_abc123XYZ"},
                "connectionTimeoutMs": 30000,
            }
        }
    )
    assert config["acme_crm"]["command"] == "npx"
    assert config["acme_crm"]["args"] == ["-y", "acme-crm-mcp"]
    assert config["acme_crm"]["env"] == {"ACME_API_TOKEN": "${MCP_ACME_CRM_ACME_API_TOKEN}"}
    assert config["acme_crm"]["connect_timeout"] == 30
    assert "connectionTimeoutMs" not in config["acme_crm"]
    assert secrets == {"MCP_ACME_CRM_ACME_API_TOKEN": "sk_live_abc123XYZ"}
    assert "sk_live_abc123XYZ" not in json.dumps(config)


def test_header_values_also_move_to_secrets_with_the_same_naming_scheme():
    config, secrets = translate_mcp_servers(
        {"custom": {"url": "https://x", "headers": {"Authorization": "Bearer sk_live_hdr"}}}
    )
    assert config["custom"]["headers"] == {"Authorization": "${MCP_CUSTOM_AUTHORIZATION}"}
    assert secrets == {"MCP_CUSTOM_AUTHORIZATION": "Bearer sk_live_hdr"}
    assert "sk_live_hdr" not in json.dumps(config)


def test_tool_filters_and_sampling_are_snake_cased_and_carry_no_secret():
    config, secrets = translate_mcp_servers(
        {
            "s": {
                "url": "https://x",
                "tools": {"include": ["a"], "exclude": ["b"]},
                "sampling": {"enabled": True, "maxTokensCap": 4096, "maxRpm": 10},
            }
        }
    )
    assert config["s"]["tools"] == {"include": ["a"], "exclude": ["b"]}
    assert config["s"]["sampling"] == {"enabled": True, "max_tokens_cap": 4096, "max_rpm": 10}
    assert secrets == {}


def test_an_unknown_key_is_dropped_not_passed_through():
    config, secrets = translate_mcp_servers({"s": {"url": "https://x", "somethingOpenClawOnly": 1}})
    assert config["s"] == {"url": "https://x"}
    assert secrets == {}


def test_empty_input_is_an_empty_map_and_no_secrets():
    assert translate_mcp_servers({}) == ({}, {})
    assert translate_mcp_servers(None) == ({}, {})


def test_oauth_field_is_dropped_no_producer_sets_it_and_hermes_never_emits_it():
    """Fix round 1, minor: no producer of this repo's ``mcp_server`` blocks sets ``oauth`` (the
    real ``mcp_remote`` shape carries ``auth: "oauth"`` instead — see the ``auth`` test above), and
    Hermes's own OpenClaw->Hermes converter (``openclaw_to_hermes.py``) never emits it either."""
    config, _ = translate_mcp_servers({"s": {"url": "https://x", "oauth": {"token": "abc"}}})
    assert "oauth" not in config["s"]


def test_a_sanitized_server_name_collision_is_rejected_not_silently_merged():
    """Minor (re-review): "acme-crm" and "acme_crm" both sanitize to MCP_ACME_CRM_TOKEN — a
    silent merge would let one server's secret name collide with (and possibly overwrite) the
    other's, which is worse than a loud, clear failure at translate time."""
    import pytest

    with pytest.raises(ValueError, match="collide"):
        translate_mcp_servers(
            {
                "acme-crm": {"command": "npx", "env": {"TOKEN": "a"}},
                "acme_crm": {"command": "npx", "env": {"TOKEN": "b"}},
            }
        )
