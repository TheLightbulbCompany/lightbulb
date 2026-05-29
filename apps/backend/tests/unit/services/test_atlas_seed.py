"""Tests for core.services.atlas_seed.pre_stage_atlas."""

from unittest.mock import MagicMock

import pytest

from core.services import atlas_seed
from core.services.atlas_seed import pre_stage_atlas


@pytest.fixture
def workspace_stub():
    ws = MagicMock()
    ws.calls = []

    def _write(user_id, path, content):
        ws.calls.append((user_id, path))
        return True

    ws.write_file_if_missing.side_effect = _write
    return ws


@pytest.fixture
def fake_files_dir(tmp_path, monkeypatch):
    root = tmp_path / "files"
    (root / "skills" / "fast-io").mkdir(parents=True)
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    (root / "build.lobster").write_text("pipeline {}", encoding="utf-8")
    (root / "skills" / "fast-io" / "SKILL.md").write_text("# fast-io", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)
    return root


@pytest.mark.asyncio
async def test_seeds_atlas_files_keyed_by_uuid(workspace_stub, fake_files_dir):
    agents = [
        {"id": "uuid-friday", "role": "ceo", "name": "Friday"},
        {"id": "uuid-atlas", "role": "general", "name": "Atlas"},
    ]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]
    assert "workspaces/uuid-atlas/.openclaw/workspace-state.json" in paths
    assert "workspaces/uuid-atlas/IDENTITY.md" in paths
    assert "workspaces/uuid-atlas/build.lobster" in paths
    assert "workspaces/uuid-atlas/skills/fast-io/SKILL.md" in paths
    assert all("uuid-friday" not in p for p in paths)


@pytest.mark.asyncio
async def test_skips_when_no_atlas(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-friday", "role": "ceo", "name": "Friday"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    workspace_stub.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_skips_junk_files(workspace_stub, tmp_path, monkeypatch):
    root = tmp_path / "files"
    (root / "__pycache__").mkdir(parents=True)
    (root / "skills" / "fast-io").mkdir(parents=True)
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    (root / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (root / ".DS_Store").write_text("junk", encoding="utf-8")
    (root / "skills" / "fast-io" / "SKILL.md").write_text("# fast-io", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]
    assert not any("__pycache__" in p for p in paths)
    assert not any(".DS_Store" in p for p in paths)
    assert "workspaces/uuid-atlas/IDENTITY.md" in paths


@pytest.mark.asyncio
async def test_per_file_error_does_not_raise_or_abort(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    calls = {"n": 0}

    def _flaky(user_id, path, content):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on a loop file, not the state write
            raise OSError("EFS hiccup")
        return True

    workspace_stub.write_file_if_missing.side_effect = _flaky
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    assert calls["n"] == 4  # state + 3 files all attempted despite the failure


@pytest.mark.asyncio
async def test_state_file_written_first(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]
    assert paths[0] == "workspaces/uuid-atlas/.openclaw/workspace-state.json", f"state first, got: {paths}"


@pytest.mark.asyncio
async def test_all_existing_emits_skipped_not_success(workspace_stub, fake_files_dir, monkeypatch):
    metrics = []
    monkeypatch.setattr(atlas_seed, "put_metric", lambda name, **kw: metrics.append(name))
    workspace_stub.write_file_if_missing.side_effect = lambda u, p, c: False
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    assert "atlas_seed.skipped_existing" in metrics
    assert "atlas_seed.success" not in metrics
