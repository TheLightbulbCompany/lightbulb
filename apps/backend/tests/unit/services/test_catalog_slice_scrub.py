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
