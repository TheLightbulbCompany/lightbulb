"""Integration test for the paperclip <-> openclaw pairing flow.

Exercises the boundary where ECS-manager fetches Paperclip's live agent
list and writes it into openclaw.json. Uses tempfile-backed EFS, so no
ECS / AWS calls.
"""

import json
import tempfile
from pathlib import Path

import pytest

from core.containers.config import write_openclaw_config


@pytest.mark.asyncio
async def test_provision_writes_paperclip_agents_into_config():
    """End-to-end: write_user_configs (via write_openclaw_config) emits
    openclaw.json with agents.list keyed by Paperclip UUIDs."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    paperclip_agents = [{"id": pc_uuid, "name": "Friday", "role": "ceo"}]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=paperclip_agents,
        )
        cfg = json.loads(path.read_text())
        agents = cfg["agents"]["list"]
        assert agents[0]["id"] == pc_uuid
        assert agents[0]["default"] is True
        assert agents[0]["workspace"] == f"/home/node/.openclaw/workspaces/{pc_uuid}"
        # Sanity: the literal "main" agent is gone.
        assert all(a["id"] != "main" for a in agents)
