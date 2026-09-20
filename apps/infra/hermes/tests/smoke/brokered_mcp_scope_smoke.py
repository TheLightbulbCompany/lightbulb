"""Prove pinned Hermes resolves brokered MCP headers from each profile's turn scope.

The infra CI image-smoke job runs this without network or model calls. A mocked
bridge test cannot prove Hermes's own MCP config loader reads secret_scope.
"""

import tempfile
from pathlib import Path

from bridge import sync, turn
from tools.mcp_tool_config import _load_mcp_config


with tempfile.TemporaryDirectory() as root:
    sync.HOME = Path(root)
    for agent_id in ("ceo", "engineering"):
        profile = sync._profile_dir(agent_id)
        profile.mkdir(parents=True)
        sync.write_profile_config(
            agent_id,
            {
                "mcp_servers": {
                    "sentry": {
                        "url": "https://mcp.sentry.dev/mcp",
                        "headers": {"Authorization": "Bearer ${MCP_SENTRY_ACCESS_TOKEN}"},
                    }
                }
            },
        )
        (profile / ".env").write_text("")

    for agent_id, token in (("ceo", "ceo-access"), ("engineering", "engineering-access")):
        with turn._hermes_home_scope(agent_id), turn._secret_scope(agent_id, {"MCP_SENTRY_ACCESS_TOKEN": token}):
            servers = _load_mcp_config()
            assert servers["sentry"]["headers"]["Authorization"] == f"Bearer {token}"

    assert "MCP_SENTRY_ACCESS_TOKEN" not in (sync._profile_dir("ceo") / ".env").read_text()
