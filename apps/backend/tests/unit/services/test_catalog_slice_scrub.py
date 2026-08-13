"""Defense-in-depth: secret-shaped values anywhere in the top-level ``tools``
block must be blanked in the published slice. Even though our credentials live
in ``skills.entries[*]`` (already scrubbed), a future mistake placing a secret
under e.g. ``tools.exec.env`` must never leak into a published artifact."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


from core.services import catalog_slice  # noqa: E402


def test_tools_exec_env_is_scrubbed_in_slice():
    openclaw_json = {
        "agents": {"list": [{"id": "agt_1", "name": "Demo"}]},
        "tools": {"exec": {"mode": "full", "env": {"SECRET": "leak_me"}}},
        "skills": {"entries": {"vercel": {"enabled": True, "apiKey": "vcp_x"}}},
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    env = slice_["tools"]["exec"]["env"]
    assert env == {"SECRET": ""}  # key preserved, value scrubbed
    # Source dict must be left untouched (deep copy).
    assert openclaw_json["tools"]["exec"]["env"] == {"SECRET": "leak_me"}
    # Non-secret keys in the tools block survive verbatim.
    assert slice_["tools"]["exec"]["mode"] == "full"


def test_plugin_entries_are_scrubbed_in_slice():
    """The plugins block used to be deep-copied with only provider entries
    dropped — a custom plugin's apiKey/env rode into the published slice
    verbatim. Entries now get the same scrub as mcp.servers/skills.entries."""
    openclaw_json = {
        "agents": {"list": [{"id": "agt_1", "name": "Demo"}]},
        "plugins": {
            "slots": {"memory": "memory-core"},
            "entries": {
                "amazon-bedrock": {"enabled": True},  # provider → dropped
                "custom-notifier": {"enabled": True, "apiKey": "sk_live_x", "env": {"TOKEN": "t"}},
            },
        },
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    entries = slice_["plugins"]["entries"]
    assert "amazon-bedrock" not in entries
    assert entries["custom-notifier"]["apiKey"] == ""
    assert entries["custom-notifier"]["env"] == {"TOKEN": ""}
    # Non-secret config + structural keys survive.
    assert entries["custom-notifier"]["enabled"] is True
    assert slice_["plugins"]["slots"] == {"memory": "memory-core"}
    # Source untouched (deep copy).
    assert openclaw_json["plugins"]["entries"]["custom-notifier"]["apiKey"] == "sk_live_x"


def test_tools_block_scrubs_nested_headers_and_preserves_structure():
    openclaw_json = {
        "agents": {"list": [{"id": "agt_1", "name": "Demo"}]},
        "tools": {
            "profile": "default",
            "fetch": {
                "headers": {"Authorization": "Bearer real", "X-Trace": "on"},
                "allowlist": ["example.com"],
            },
        },
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    tools = slice_["tools"]
    assert tools["fetch"]["headers"] == {"Authorization": "", "X-Trace": ""}
    # Non-secret nested data preserved.
    assert tools["profile"] == "default"
    assert tools["fetch"]["allowlist"] == ["example.com"]
