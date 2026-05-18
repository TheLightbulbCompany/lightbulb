"""Tests for catalog_slice."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import catalog_slice  # noqa: E402


def test_extract_agent_slice_strips_provider_plugins():
    """Provider plugins are user-config (the buyer picked a provider at
    signup), not agent-config. Shipping them in the slice would deep-merge
    over the buyer's provider setup and silently switch them off the path
    they chose. Strip at slice time."""
    openclaw_json = {
        "agents": {"list": [{"id": "agt_1", "name": "Demo"}]},
        "plugins": {
            "entries": {
                "amazon-bedrock": {"enabled": True, "config": {"region": "us-east-1"}},
                "chatgpt-oauth": {"enabled": False},
                "anthropic": {"enabled": True},
                "openai": {"enabled": True},
                "browser": {"enabled": True},
                "memory": {"enabled": True},
            }
        },
        "tools": {"profile": "default"},
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    entries = slice_["plugins"]["entries"]
    # Provider plugins gone.
    assert "amazon-bedrock" not in entries
    assert "chatgpt-oauth" not in entries
    assert "anthropic" not in entries
    assert "openai" not in entries
    # Non-provider plugins (genuinely agent-scoped) preserved.
    assert entries["browser"] == {"enabled": True}
    assert entries["memory"] == {"enabled": True}


def test_extract_agent_slice_handles_missing_plugins_block():
    """openclaw_json without a plugins block (legacy / fresh container)
    must still produce a valid slice — empty plugins dict, no crash."""
    openclaw_json = {"agents": {"list": [{"id": "agt_1", "name": "Demo"}]}}
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    assert slice_["plugins"] == {}
