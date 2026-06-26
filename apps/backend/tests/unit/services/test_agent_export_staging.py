"""Tests for agent_export.export_built_agent_from_staging — the Atlas
agent-builder snapshot path.

Unlike the seller path (``export_agent_from_efs``), the built agent is
self-describing: the skeleton-generator nests the agent's own ``openclaw.json``
+ persona files under a ``workspace/`` subdir of the staging dir, with a
``build-manifest.json`` at the staging-dir root. The export tars the inner
``workspace/`` (files land at the tarball root) and builds the slice from the
staging dir's OWN config — never the owner's root openclaw.json.
"""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import agent_export  # noqa: E402


def _staging_openclaw_config() -> dict:
    """A skeleton-generator-shaped openclaw.json for the built agent.

    Mirrors atlas-skeleton-generator.js ``generateOpenclawJson``: the agent's
    id IS its display name, skills live on the agent entry + skills.entries,
    and lobster/llm-task plugins are enabled.
    """
    return {
        "agents": {
            "defaults": {"workspace": "~/.openclaw/agents/lex-legal/workspace"},
            "list": [
                {
                    "id": "lex-legal",
                    "workspace": "~/.openclaw/agents/lex-legal/workspace",
                    "model": "claude-haiku-4-5",
                    "archetype": "general",
                    "skills": ["gog", "slack", "contract-review"],
                    "tools": {"profile": "coding", "alsoAllow": ["lobster", "llm-task"]},
                }
            ],
        },
        "plugins": {
            "entries": {
                "lobster": {"enabled": True},
                "llm-task": {"enabled": True, "config": {"defaultModel": "claude-haiku-4-5"}},
            }
        },
        "skills": {"entries": {"contract-review": {"enabled": True}}},
        "tools": {"deny": []},
    }


@pytest.fixture
def fake_staging(tmp_path: Path):
    """Builds /tmp/<root>/users/<owner>/workspaces/<staging-id>/ with the
    skeleton-generator on-disk layout (files under workspace/, build-manifest
    at staging root) and patches get_workspace() to anchor at <root>/users.
    """
    owner_id = "org_owner_abc"
    staging_agent_id = "atlas-staging-build-001"

    user_root = tmp_path / "users" / owner_id
    staging_dir = user_root / "workspaces" / staging_agent_id
    workspace_subdir = staging_dir / "workspace"
    workspace_subdir.mkdir(parents=True)

    # The built agent's OWN config + persona files — all under workspace/.
    (workspace_subdir / "openclaw.json").write_text(json.dumps(_staging_openclaw_config()), encoding="utf-8")
    (workspace_subdir / "AGENTS.md").write_text("# AGENTS.md\n\nLex the legal bot.\n", encoding="utf-8")
    (workspace_subdir / "SOUL.md").write_text("# SOUL.md\n", encoding="utf-8")

    # build-manifest.json lives at the staging ROOT (outside workspace/) and
    # must NOT ship in the tarball.
    (staging_dir / "build-manifest.json").write_text(
        json.dumps({"build_id": "build-001", "agent_name": "lex-legal", "files": []}),
        encoding="utf-8",
    )

    from core.containers import workspace as workspace_module

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        yield owner_id, staging_agent_id, staging_dir


def _list_tar_names(tar_bytes: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def test_staging_export_tars_workspace_subdir_at_root(fake_staging):
    """Files under workspace/ land at the tarball ROOT (./openclaw.json,
    ./AGENTS.md) — NOT nested under workspace/, and build-manifest.json
    (staging root) never ships."""
    owner_id, staging_agent_id, _ = fake_staging
    pkg = agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id=staging_agent_id)
    assert pkg.format == "openclaw"
    names = _list_tar_names(pkg.tarball_bytes)

    # Shippable files are at the tarball root.
    assert "./openclaw.json" in names, names
    assert "./AGENTS.md" in names, names
    # Not nested under a workspace/ prefix.
    assert not any(n.startswith("./workspace/") for n in names), names
    # build-manifest.json (staging root, outside workspace/) is excluded.
    assert not any("build-manifest.json" in n for n in names), names


def test_staging_export_slice_from_own_config(fake_staging):
    """The slice carries the built agent's dict (skills + identity) and the
    plugins block, sourced from the staging dir's OWN openclaw.json."""
    owner_id, staging_agent_id, _ = fake_staging
    pkg = agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id=staging_agent_id)

    # Manifest keys off the built agent's id (= its display name).
    assert pkg.manifest["agent_id"] == "lex-legal"
    assert pkg.manifest["name"] == "lex-legal"
    assert "safety_flags" in pkg.manifest
    assert pkg.manifest["file_count"] == len(pkg.tarball_contents)

    slice_ = pkg.openclaw_slice
    assert slice_, "openclaw_slice must be populated"

    agent = slice_["agent"]
    # The agent dict carries its skills; user-specific fields are stripped.
    assert agent.get("skills") == ["gog", "slack", "contract-review"]
    assert "model" not in agent, "model must be stripped from slice"
    assert "workspace" not in agent, "workspace must be stripped from slice"
    assert "id" not in agent, "id must be stripped (regenerated per-deploy)"

    # Plugins block travels (non-provider lobster/llm-task survive the strip).
    assert slice_["plugins"]["entries"]["lobster"] == {"enabled": True}
    assert "llm-task" in slice_["plugins"]["entries"]


def test_staging_export_falls_back_to_root_openclaw_json(tmp_path: Path):
    """Defensive: if there's no workspace/ subdir, tar the staging root and
    read the root-level openclaw.json instead."""
    owner_id = "org_owner_flat"
    staging_agent_id = "flat-staging-001"
    user_root = tmp_path / "users" / owner_id
    staging_dir = user_root / "workspaces" / staging_agent_id
    staging_dir.mkdir(parents=True)
    (staging_dir / "openclaw.json").write_text(json.dumps(_staging_openclaw_config()), encoding="utf-8")
    (staging_dir / "AGENTS.md").write_text("# flat\n", encoding="utf-8")

    from core.containers import workspace as workspace_module

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        pkg = agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id=staging_agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert "./openclaw.json" in names, names
    assert "./AGENTS.md" in names, names
    assert pkg.manifest["agent_id"] == "lex-legal"


def test_staging_export_missing_config_raises(tmp_path: Path):
    """A staging dir with no parseable openclaw.json raises BuiltAgentConfigError
    (router maps to 400)."""
    owner_id = "org_owner_nocfg"
    staging_agent_id = "nocfg-staging-001"
    user_root = tmp_path / "users" / owner_id
    workspace_subdir = user_root / "workspaces" / staging_agent_id / "workspace"
    workspace_subdir.mkdir(parents=True)
    (workspace_subdir / "AGENTS.md").write_text("# no config here\n", encoding="utf-8")

    from core.containers import workspace as workspace_module

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        with pytest.raises(agent_export.BuiltAgentConfigError, match="no parseable openclaw.json"):
            agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id=staging_agent_id)


def test_staging_export_missing_dir_raises_not_found(fake_staging):
    """A staging_agent_id with no workspace dir 404s (AgentNotFoundError)."""
    owner_id, _, _ = fake_staging
    with pytest.raises(agent_export.AgentNotFoundError):
        agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id="never-built-this")


def test_staging_export_invalid_id_rejected(fake_staging):
    """A path-traversal / malformed staging id raises InvalidAgentIdError."""
    owner_id, _, _ = fake_staging
    with pytest.raises(agent_export.InvalidAgentIdError):
        agent_export.export_built_agent_from_staging(owner_id=owner_id, staging_agent_id="../escape")
