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


@pytest.mark.asyncio
async def test_provision_then_chat_create_yields_single_uuid_per_agent():
    """End-to-end: after provisioning with one Paperclip Main Agent and
    creating a second agent via /chat, the openclaw.json reflects both
    agents keyed by Paperclip UUIDs — not OpenClaw-internal UUIDs."""
    main_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    second_uuid = "11111111-1111-1111-1111-111111111111"

    # Phase 1: provision-time write picks up Friday (the seeded Main Agent).
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "openclaw.json"
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=[
                {"id": main_uuid, "name": "Friday", "role": "ceo"},
            ],
        )
        cfg1 = json.loads(path.read_text())
        assert [a["id"] for a in cfg1["agents"]["list"]] == [main_uuid]

        # Phase 2: simulating that the second agent was created via
        # /chat and registered in the container via agents.create RPC.
        # On next reprovision, list_agents would now return both.
        await write_openclaw_config(
            config_path=path,
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            user_id="user_abc",
            paperclip_agents=[
                {"id": main_uuid, "name": "Friday", "role": "ceo"},
                {"id": second_uuid, "name": "Code Reviewer", "role": "engineer"},
            ],
        )
        cfg2 = json.loads(path.read_text())
        assert sorted(a["id"] for a in cfg2["agents"]["list"]) == sorted([main_uuid, second_uuid])
        # Default still on the CEO.
        defaults = [a for a in cfg2["agents"]["list"] if a.get("default")]
        assert len(defaults) == 1
        assert defaults[0]["id"] == main_uuid


@pytest.mark.asyncio
async def test_friday_identity_files_seeded_at_workspace_path(monkeypatch, tmp_path):
    """End-to-end: pre_stage_friday_identity drops IDENTITY/SOUL/USER/BOOTSTRAP
    at /{owner_id}/workspaces/<friday_uuid>/ on EFS with the right content."""
    from core.config import settings
    from core.containers.workspace import Workspace
    from core.services import friday_identity

    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    # Stub profile resolution: tests don't depend on Clerk in this smoke test.
    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
            "provider_label": "Bedrock (Claude, Isol8-managed credits)",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    owner_id = "user_smoke"
    friday_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    paperclip_agents = [{"id": friday_uuid, "role": "ceo", "name": "Friday"}]
    workspace = Workspace(mount_path=str(tmp_path))

    await friday_identity.pre_stage_friday_identity(
        owner_id=owner_id,
        paperclip_agents=paperclip_agents,
        workspace=workspace,
    )

    base = tmp_path / owner_id / "workspaces" / friday_uuid
    assert (base / "IDENTITY.md").exists()
    assert (base / "SOUL.md").exists()
    assert (base / "USER.md").exists()
    assert (base / "BOOTSTRAP.md").exists()

    # Sanity-check the substantive content.
    soul = (base / "SOUL.md").read_text()
    assert "You are Friday" in soul
    assert "Sam's AI company" in soul

    user_md = (base / "USER.md").read_text()
    assert "sam@example.com" in user_md

    identity = (base / "IDENTITY.md").read_text()
    assert "Name:** Friday" in identity


@pytest.mark.asyncio
async def test_friday_identity_skipped_on_reprovision(monkeypatch, tmp_path):
    """Calling the seeder twice (simulated reprovision) doesn't clobber files."""
    from core.config import settings
    from core.containers.workspace import Workspace
    from core.services import friday_identity

    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
            "provider_label": "Bedrock (Claude, Isol8-managed credits)",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    owner_id = "user_smoke"
    friday_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    paperclip_agents = [{"id": friday_uuid, "role": "ceo"}]
    workspace = Workspace(mount_path=str(tmp_path))

    # First provision.
    await friday_identity.pre_stage_friday_identity(
        owner_id=owner_id,
        paperclip_agents=paperclip_agents,
        workspace=workspace,
    )

    # User edits SOUL.md to add personal flair (simulates Friday's evolution).
    soul_path = tmp_path / owner_id / "workspaces" / friday_uuid / "SOUL.md"
    soul_path.write_text("CUSTOM SOUL — user edited this")

    # Second provision (reprovision).
    await friday_identity.pre_stage_friday_identity(
        owner_id=owner_id,
        paperclip_agents=paperclip_agents,
        workspace=workspace,
    )

    # User's edit is preserved.
    assert soul_path.read_text() == "CUSTOM SOUL — user edited this"
