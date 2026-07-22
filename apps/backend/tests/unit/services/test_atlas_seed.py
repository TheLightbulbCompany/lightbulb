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


@pytest.fixture
def captured_ws():
    """Workspace stub that records (path -> content) AND emulates the real
    ``write_file_if_missing`` skip-if-exists semantics — first write wins,
    later writes to the same path no-op. That's what makes the ACP pre-write
    of AGENTS.md beat the generic loop's vanilla write."""
    ws = MagicMock()
    ws.written = {}

    def _write(user_id, path, content):
        if path in ws.written:
            return False
        ws.written[path] = content
        return True

    ws.write_file_if_missing.side_effect = _write
    return ws


@pytest.mark.asyncio
async def test_seeds_atlas_files_keyed_by_uuid(workspace_stub, fake_files_dir):
    agents = [
        {"id": "uuid-friday", "role": "ceo", "name": "Friday"},
        {"id": "uuid-atlas", "role": "general", "name": "Atlas"},
    ]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]
    assert "workspace-uuid-atlas/.openclaw/workspace-state.json" in paths
    assert "workspace-uuid-atlas/IDENTITY.md" in paths
    assert "workspace-uuid-atlas/build.lobster" in paths
    assert "workspace-uuid-atlas/skills/fast-io/SKILL.md" in paths
    assert all("uuid-friday" not in p for p in paths)


@pytest.mark.asyncio
async def test_skips_when_no_atlas(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-friday", "role": "ceo", "name": "Friday"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    workspace_stub.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_ambiguous_atlas(workspace_stub, fake_files_dir):
    """Two agents named Atlas (e.g. seeded Atlas renamed + a user-created one)
    => refuse to seed, so a custom agent's workspace is never polluted."""
    agents = [
        {"id": "uuid-1", "role": "general", "name": "Atlas"},
        {"id": "uuid-2", "role": "general", "name": "Atlas"},
    ]
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
    assert "workspace-uuid-atlas/IDENTITY.md" in paths


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
    assert paths[0] == "workspace-uuid-atlas/.openclaw/workspace-state.json", f"state first, got: {paths}"


@pytest.mark.asyncio
async def test_all_existing_emits_skipped_not_success(workspace_stub, fake_files_dir, monkeypatch):
    metrics = []
    monkeypatch.setattr(atlas_seed, "put_metric", lambda name, **kw: metrics.append(name))
    workspace_stub.write_file_if_missing.side_effect = lambda u, p, c: False
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    assert "atlas_seed.skipped_existing" in metrics
    assert "atlas_seed.success" not in metrics


def _acp_files_dir(tmp_path, monkeypatch):
    root = tmp_path / "files"
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("# AGENTS.md\n\nbase recruiter copy\n", encoding="utf-8")
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)
    return root


@pytest.mark.asyncio
async def test_acp_section_appended_to_agents_when_flag_on(captured_ws, tmp_path, monkeypatch):
    """With ACP_ENABLED on, Atlas's AGENTS.md ships the harness-delegation
    section appended to the vendored base copy."""
    _acp_files_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.settings.ACP_ENABLED", True)
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]

    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=captured_ws)

    agents_md = captured_ws.written["workspace-uuid-atlas/AGENTS.md"]
    assert "base recruiter copy" in agents_md  # vendored base preserved
    assert "Coding-Harness Delegation (ACP)" in agents_md  # section appended
    assert 'sessions_spawn({ runtime: "acp" })' in agents_md


@pytest.mark.asyncio
async def test_acp_section_absent_when_flag_off(captured_ws, tmp_path, monkeypatch):
    """Default (flag off): AGENTS.md is the vanilla vendored copy — no ACP
    section leaks into the recruiter prompt before the feature is rolled out."""
    _acp_files_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.settings.ACP_ENABLED", False)
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]

    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=captured_ws)

    agents_md = captured_ws.written["workspace-uuid-atlas/AGENTS.md"]
    assert "base recruiter copy" in agents_md
    assert "Coding-Harness Delegation (ACP)" not in agents_md


def _bridge_files_dir(tmp_path, monkeypatch):
    """Files dir whose build.lobster carries the {{ISOL8_BRIDGE_URL}} placeholder
    that pre_stage_atlas substitutes at stage time."""
    root = tmp_path / "files"
    root.mkdir(parents=True)
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    (root / "build.lobster").write_text(
        'name: build\nsteps:\n  - id: register\n    command: curl -sS -X POST "{{ISOL8_BRIDGE_URL}}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env, expected_host",
    [
        ("dev", "https://api-dev.isol8.co"),
        ("staging", "https://api-staging.isol8.co"),
        ("prod", "https://api.isol8.co"),
        ("production", "https://api.isol8.co"),  # "production" alias → prod host
        ("", "https://api.isol8.co"),  # empty env resolves to the prod host
    ],
)
async def test_bridge_url_substituted_into_build_lobster(captured_ws, tmp_path, monkeypatch, env, expected_host):
    """pre_stage rewrites build.lobster's {{ISOL8_BRIDGE_URL}} placeholder to the
    concrete per-env, per-owner agent-builder bridge URL, leaving none behind."""
    _bridge_files_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", env)
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]

    await pre_stage_atlas(owner_id="org_abc", paperclip_agents=agents, workspace=captured_ws)

    build_lobster = captured_ws.written["workspace-uuid-atlas/build.lobster"]
    assert "{{ISOL8_BRIDGE_URL}}" not in build_lobster
    assert f"{expected_host}/api/v1/agent-builder/u/org_abc/register-built-agent" in build_lobster


@pytest.mark.asyncio
async def test_bridge_url_no_placeholder_leaves_loop_to_write(workspace_stub, fake_files_dir):
    """When build.lobster has no placeholder, the templating helper no-ops and the
    generic loop writes the file once (no double write)."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    build_writes = [p for (_u, p) in workspace_stub.calls if p.endswith("/build.lobster")]
    assert build_writes == ["workspace-uuid-atlas/build.lobster"]


@pytest.fixture
def force_ws():
    """Workspace stub that distinguishes write_file (unconditional overwrite)
    from write_file_if_missing (skip-if-exists). write_file_if_missing always
    returns False (pretend every file already exists) so that the only way a
    file gets staged is via write_file — proving the force path overwrites
    where the default path would skip."""
    ws = MagicMock()
    ws.overwrote = []  # paths written via write_file
    ws.if_missing_calls = []  # paths attempted via write_file_if_missing

    def _write_file(user_id, path, content):
        ws.overwrote.append(path)
        return None

    def _write_if_missing(user_id, path, content):
        ws.if_missing_calls.append(path)
        return False  # already exists -> default path would skip

    ws.write_file.side_effect = _write_file
    ws.write_file_if_missing.side_effect = _write_if_missing
    return ws


@pytest.mark.asyncio
async def test_force_overwrites_existing_files(force_ws, fake_files_dir):
    """force=True overwrites every staged file via write_file even when the file
    already exists (where write_file_if_missing would skip) — so an existing
    container picks up the rewritten bundle. write_file_if_missing is never used."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=force_ws, force=True)
    # Force path NEVER touches the skip-if-exists writer.
    assert force_ws.if_missing_calls == []
    # IDENTITY.md exists (write_if_missing would return False) yet was overwritten.
    assert "workspace-uuid-atlas/IDENTITY.md" in force_ws.overwrote
    # workspace-state.json + the 3 bundle files (IDENTITY.md, build.lobster,
    # skills/fast-io/SKILL.md) all force-written.
    assert "workspace-uuid-atlas/.openclaw/workspace-state.json" in force_ws.overwrote
    assert result["force"] is True
    assert result["reason"] is None
    assert result["overwritten"] == len(force_ws.overwrote) == 4


@pytest.mark.asyncio
async def test_force_does_not_clobber_substituted_build_lobster(force_ws, monkeypatch, tmp_path):
    """Under force, the URL-substituted build.lobster from the pre-write must
    survive — the generic loop must NOT overwrite it with the placeholder copy."""
    _bridge_files_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "dev")
    # Capture the last-written content per path (write_file overwrites) while
    # still recording the path in overwrote (the fixture's default behavior).
    written: dict[str, str] = {}

    def _wf(user_id, path, content):
        force_ws.overwrote.append(path)
        written[path] = content

    force_ws.write_file.side_effect = _wf
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="org_abc", paperclip_agents=agents, workspace=force_ws, force=True)
    build = written["workspace-uuid-atlas/build.lobster"]
    assert "{{ISOL8_BRIDGE_URL}}" not in build
    assert "https://api-dev.isol8.co/api/v1/agent-builder/u/org_abc/register-built-agent" in build


@pytest.mark.asyncio
async def test_force_with_acp_overwrites_agents_with_acp_section(force_ws, monkeypatch, tmp_path):
    """force=True AND ACP_ENABLED=True: AGENTS.md is force-overwritten WITH the
    ACP section appended (force must carry the augmentation, not the vanilla
    copy). Also asserts written stays 0 under force (overwrites count in
    overwritten only, so atlas_seed.success doesn't fire on overwrites)."""
    _acp_files_dir(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.settings.ACP_ENABLED", True)
    written: dict[str, str] = {}

    def _wf(user_id, path, content):
        force_ws.overwrote.append(path)
        written[path] = content

    force_ws.write_file.side_effect = _wf
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=force_ws, force=True)

    agents_md = written["workspace-uuid-atlas/AGENTS.md"]
    assert "base recruiter copy" in agents_md  # vendored base preserved under force
    assert "Coding-Harness Delegation (ACP)" in agents_md  # augmentation carried under force
    assert force_ws.if_missing_calls == []  # never the skip-writer
    assert result["force"] is True
    assert result["written"] == 0  # force → no "new write" accounting, all in overwritten
    assert result["overwritten"] == len(force_ws.overwrote)
