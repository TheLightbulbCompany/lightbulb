"""Tests for the orphan provider-level channel-credential cleanup script."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from scripts.cleanup_orphan_channel_creds import find_orphan_provider_secrets  # noqa: E402


def test_finds_provider_level_secrets_only():
    config = {
        "channels": {
            "slack": {
                "enabled": True,
                "mode": "socket",
                "appToken": "xapp-orphan",
                "botToken": "xoxb-orphan",
                "accounts": {"a": {"botToken": "xoxb-real"}},  # real account ignored
            },
            "telegram": {"enabled": True, "accounts": {}},  # clean
            "discord": {"enabled": True, "token": "disc-orphan"},
        }
    }
    found = find_orphan_provider_secrets(config)
    assert set(found["slack"]) == {"appToken", "botToken"}
    assert found["discord"] == ["token"]
    assert "telegram" not in found  # no provider-level secret


def test_returns_empty_when_clean():
    config = {"channels": {"telegram": {"enabled": True, "accounts": {"a": {"botToken": "real"}}}}}
    assert find_orphan_provider_secrets(config) == {}


def test_handles_missing_or_malformed_channels():
    assert find_orphan_provider_secrets({}) == {}
    assert find_orphan_provider_secrets({"channels": None}) == {}
    assert find_orphan_provider_secrets({"channels": {"slack": "nope"}}) == {}
