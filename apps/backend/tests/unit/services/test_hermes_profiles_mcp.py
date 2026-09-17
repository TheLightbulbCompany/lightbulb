from core.services.hermes_profiles import translate_mcp_servers


def test_remote_oauth_server_keeps_auth_oauth_and_snake_cases_timeouts():
    out = translate_mcp_servers(
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
    assert out["stripe"] == {
        "url": "https://mcp.stripe.com",
        "auth": "oauth",
        "timeout": 60,
        "connect_timeout": 15,
    }


def test_stdio_server_copies_command_args_env_and_converts_timeout():
    out = translate_mcp_servers(
        {
            "qbo": {
                "command": "npx",
                "args": ["-y", "qbo-mcp"],
                "env": {"QBO_TOKEN_STORE": "/opt/data/profiles/a1/qbo.json"},
                "connectionTimeoutMs": 30000,
            }
        }
    )
    assert out["qbo"]["command"] == "npx"
    assert out["qbo"]["args"] == ["-y", "qbo-mcp"]
    assert out["qbo"]["env"] == {"QBO_TOKEN_STORE": "/opt/data/profiles/a1/qbo.json"}
    assert out["qbo"]["connect_timeout"] == 30
    assert "connectionTimeoutMs" not in out["qbo"]


def test_tool_filters_and_sampling_are_snake_cased():
    out = translate_mcp_servers(
        {
            "s": {
                "url": "https://x",
                "tools": {"include": ["a"], "exclude": ["b"]},
                "sampling": {"enabled": True, "maxTokensCap": 4096, "maxRpm": 10},
            }
        }
    )
    assert out["s"]["tools"] == {"include": ["a"], "exclude": ["b"]}
    assert out["s"]["sampling"] == {"enabled": True, "max_tokens_cap": 4096, "max_rpm": 10}


def test_an_unknown_key_is_dropped_not_passed_through():
    out = translate_mcp_servers({"s": {"url": "https://x", "somethingOpenClawOnly": 1}})
    assert out["s"] == {"url": "https://x"}


def test_empty_input_is_an_empty_map():
    assert translate_mcp_servers({}) == {}
    assert translate_mcp_servers(None) == {}
