"""Tests for openclaw.json secret-field redaction (CEO S3)."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def test_redacts_key_suffixed_fields():
    from core.services.admin_redact import redact_openclaw_config

    config = {
        "providers": {
            "anthropic_api_key": "sk-ant-abc",
            "openai_api_key": "sk-openai-def",
            "webhook_url": "https://x",
        },
        "ok_value": "kept",
    }
    out = redact_openclaw_config(config)
    assert out["providers"]["anthropic_api_key"] == "***redacted***"
    assert out["providers"]["openai_api_key"] == "***redacted***"
    assert out["providers"]["webhook_url"] == "***redacted***"
    assert out["ok_value"] == "kept"


def test_redacts_nested_secret_fields():
    from core.services.admin_redact import redact_openclaw_config

    config = {
        "outer": {
            "middle": {
                "anthropic_secret": "shh",
                "stripe_token": "tok_xyz",
                "user_password": "p4ss",
                "normal": 42,
            },
        },
    }
    out = redact_openclaw_config(config)
    assert out["outer"]["middle"]["anthropic_secret"] == "***redacted***"
    assert out["outer"]["middle"]["stripe_token"] == "***redacted***"
    assert out["outer"]["middle"]["user_password"] == "***redacted***"
    assert out["outer"]["middle"]["normal"] == 42


def test_redacts_inside_list_elements():
    from core.services.admin_redact import redact_openclaw_config

    config = {
        "providers_list": [
            {"name": "openai", "api_key": "sk-x"},
            {"name": "anthropic", "api_key": "sk-y"},
        ],
    }
    out = redact_openclaw_config(config)
    assert out["providers_list"][0]["api_key"] == "***redacted***"
    assert out["providers_list"][1]["api_key"] == "***redacted***"
    assert out["providers_list"][0]["name"] == "openai"


def test_does_not_touch_non_matching_keys():
    from core.services.admin_redact import redact_openclaw_config

    config = {"name": "myagent", "model": "claude-opus-4-6-v1", "tools": ["exec", "browser"]}
    out = redact_openclaw_config(config)
    assert out == config


def test_redacts_bearer_exact_match():
    from core.services.admin_redact import redact_openclaw_config

    out = redact_openclaw_config({"bearer": "abcdef"})
    assert out["bearer"] == "***redacted***"


def test_does_not_redact_innocent_substrings():
    """`_key` suffix only — `keystone` or `keyword` should NOT redact."""
    from core.services.admin_redact import redact_openclaw_config

    out = redact_openclaw_config({"keystone": "ok", "keyword": "phrase"})
    assert out["keystone"] == "ok"
    assert out["keyword"] == "phrase"


def test_case_insensitive_key_matching():
    from core.services.admin_redact import redact_openclaw_config

    out = redact_openclaw_config({"ANTHROPIC_API_KEY": "secret", "Bearer": "tok"})
    assert out["ANTHROPIC_API_KEY"] == "***redacted***"
    assert out["Bearer"] == "***redacted***"


def test_idempotent_on_already_redacted():
    """Running the redactor twice is a no-op."""
    from core.services.admin_redact import redact_openclaw_config

    once = redact_openclaw_config({"anthropic_api_key": "sk-x", "name": "ok"})
    twice = redact_openclaw_config(once)
    assert once == twice


def test_passes_scalars_through():
    from core.services.admin_redact import redact_openclaw_config

    assert redact_openclaw_config("hello") == "hello"
    assert redact_openclaw_config(42) == 42
    assert redact_openclaw_config(None) is None
    assert redact_openclaw_config(True) is True


def test_empty_structures():
    from core.services.admin_redact import redact_openclaw_config

    assert redact_openclaw_config({}) == {}
    assert redact_openclaw_config([]) == []
