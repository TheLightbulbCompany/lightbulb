"""Tests for catalog_slice."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


from core.services import catalog_slice  # noqa: E402
from core.services.catalog_slice import _scrub_secret_fields  # noqa: E402


def test_scrub_blanks_value_after_secret_flag():
    entry = {"command": "npx", "args": ["-y", "instantly-mcp", "--api-key", "REALSECRETVALUE123"]}
    out = _scrub_secret_fields(entry)
    assert out["args"] == ["-y", "instantly-mcp", "--api-key", ""]
    # original untouched (deep copy)
    assert entry["args"][-1] == "REALSECRETVALUE123"


def test_scrub_blanks_joined_secret_flag():
    entry = {"command": "x", "args": ["--token=abc123DEF456ghi789"]}
    assert _scrub_secret_fields(entry)["args"] == ["--token="]


def test_scrub_leaves_non_secret_flags_and_packages_untouched():
    entry = {"command": "npx", "args": ["-y", "@perplexity-ai/mcp-server", "--transport", "stdio"]}
    assert _scrub_secret_fields(entry)["args"] == ["-y", "@perplexity-ai/mcp-server", "--transport", "stdio"]


def test_scrub_still_blanks_named_fields():
    entry = {"apiKey": "k", "env": {"X": "y"}, "args": ["ok"]}
    out = _scrub_secret_fields(entry)
    assert out["apiKey"] == "" and out["env"] == {"X": ""} and out["args"] == ["ok"]


def test_scrub_leaves_non_secret_eq_args():
    # A non-secret `key=value` arg must not be over-scrubbed by the `=` branch.
    entry = {"args": ["--config=foo.json"]}
    assert _scrub_secret_fields(entry)["args"] == ["--config=foo.json"]


def test_scrub_trailing_secret_flag_no_value():
    # A secret flag with no following value must not raise or misbehave.
    entry = {"args": ["--api-key"]}
    assert _scrub_secret_fields(entry)["args"] == ["--api-key"]


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


class TestSanitizeAgentEntryForSchema:
    """Whitelist sanitizer against the strict AgentEntrySchema. One key outside
    the schema fails validation for the WHOLE config and takes the owner's
    gateway down — sources of entries (Atlas-built staging configs, seller
    exports) are not trusted to be schema-shaped."""

    def test_drops_invented_and_retired_keys_and_reports_them(self):
        from core.services.catalog_slice import sanitize_agent_entry_for_schema

        entry = {
            "id": "a1",
            "name": "Jarvis",
            "skills": ["search"],
            "archetype": "engineer",  # invented by Atlas's skeleton generator
            "systemPromptOverride": "be terse",  # retired upstream
        }
        sanitized, dropped = sanitize_agent_entry_for_schema(entry)
        assert sanitized == {"id": "a1", "name": "Jarvis", "skills": ["search"]}
        assert dropped == ["archetype", "systemPromptOverride"]
        # Input is not mutated (deep-copy semantics).
        assert "archetype" in entry

    def test_every_key_the_whitelist_still_allows_passes_through(self):
        """``ALLOWED_AGENT_ENTRY_KEYS`` is the 7.2 whitelist, and the image gate that used to
        pick between it and the narrower 8.1 one is gone with the pin it read -- so the three
        keys 8.1 retired are dropped unconditionally and everything else survives."""
        from core.services.catalog_slice import (
            ALLOWED_AGENT_ENTRY_KEYS,
            _REMOVED_AT_2026_8_1,
            sanitize_agent_entry_for_schema,
        )

        entry = {k: "x" for k in ALLOWED_AGENT_ENTRY_KEYS}
        sanitized, dropped = sanitize_agent_entry_for_schema(entry)
        assert set(dropped) == _REMOVED_AT_2026_8_1
        assert set(sanitized) == ALLOWED_AGENT_ENTRY_KEYS - _REMOVED_AT_2026_8_1


class TestRemovedAtEightOne:
    """The whitelist is derived from the 7.2 AgentEntrySchema. A slice extracted from a 7.2-era
    container -- a marketplace republish or a backup import -- carries entry keys 2026.8.1
    retired, and deep-merging one into a strict config crash-loops the reload. The image gate
    that decided whether to narrow the whitelist is gone with the pin it read, so they are
    dropped unconditionally.
    """

    ENTRY = {
        "id": "agent_x",
        "name": "X",
        "skills": ["search"],
        "models": {"openai/gpt-5.5": {}},
        "runRetries": {"max": 8},
        "memorySearch": {"enabled": True},
        "contextTokens": 200_000,
    }

    def test_the_three_retired_keys_are_dropped_and_nothing_else_is(self):
        from core.services import catalog_slice

        sanitized, dropped = catalog_slice.sanitize_agent_entry_for_schema(dict(self.ENTRY))
        assert set(dropped) == {"runRetries", "memorySearch", "contextTokens"}
        # `models` is still valid on an entry at 8.1 (AgentModelRuntimeEntrySchema) -- verified
        # against the vendored schema, not assumed. Dropping it would silently strip a seller's
        # per-model overrides from every deploy.
        assert sanitized["models"] == {"openai/gpt-5.5": {}}
        assert sanitized["skills"] == ["search"]

    def test_the_input_entry_is_never_mutated(self):
        """Callers hand it a slice they go on to use -- ``extract_agent_slice`` sanitizes for the
        manifest while the publish path keeps working from the original."""
        from core.services import catalog_slice

        entry = dict(self.ENTRY)
        catalog_slice.sanitize_agent_entry_for_schema(entry)
        assert entry == self.ENTRY
