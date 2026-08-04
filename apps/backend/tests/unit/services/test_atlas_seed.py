"""Tests for core.services.atlas_seed.pre_stage_atlas."""

import re
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.services import atlas_seed
from core.services.atlas_seed import pre_stage_atlas

# JS-canonical ISO-8601: exactly 3 fractional digits + literal ``Z`` — the only
# shape OpenClaw 7.2's workspace-state migration accepts. Python's
# ``.isoformat()`` (6-digit micros + ``+00:00``) is rejected and bricks agent runs.
_JS_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _assert_js_canonical_roundtrip(ts: str) -> None:
    assert _JS_ISO_RE.match(ts), f"{ts!r} is not JS-canonical (YYYY-MM-DDTHH:MM:SS.mmmZ)"
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    reformatted = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    assert reformatted == ts


def _mark_workspace_fresh(ws):
    """seed_workspace_state's one-shot gate: a workspace dir that does not
    exist yet means this provision creates it, so the state marker is seeded.
    (An existing dir means reprovision — the marker is never re-written; see
    friday_identity.seed_workspace_state, #1429.)"""
    ws.agent_workspace_path.return_value.is_dir.return_value = False
    return ws


@pytest.fixture
def workspace_stub():
    ws = _mark_workspace_fresh(MagicMock())
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
    ws = _mark_workspace_fresh(MagicMock())
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
    assert "workspace-uuid-atlas/.openclaw/isol8-setup-seed.json" in paths
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
async def test_skips_vendored_suites_and_docs_but_keeps_atlas_own_markdown(workspace_stub, tmp_path, monkeypatch):
    """Vendored skill bundles carry upstream test suites + packaging docs that
    nothing in a workspace reads. They must not be staged — but the basename
    rule must never reach Atlas's own top-level identity markdown."""
    root = tmp_path / "files"
    bundle = root / "skills" / "capability-evolver"
    (bundle / "test").mkdir(parents=True)
    (bundle / "src").mkdir(parents=True)
    # Atlas's own files — load-bearing, must survive.
    (root / "AGENTS.md").write_text("# Atlas", encoding="utf-8")
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    # Vendored bundle: SKILL.md + code stay, suite + upstream docs go.
    (bundle / "SKILL.md").write_text("# evolver", encoding="utf-8")
    (bundle / "index.js").write_text("module.exports={}", encoding="utf-8")
    (bundle / "package.json").write_text("{}", encoding="utf-8")
    (bundle / "src" / "config.js").write_text("//x", encoding="utf-8")
    (bundle / "test" / "ops.test.js").write_text("//t", encoding="utf-8")
    (bundle / "README.md").write_text("# upstream", encoding="utf-8")
    (bundle / "README.zh-CN.md").write_text("# 上游", encoding="utf-8")
    (bundle / "CONTRIBUTING.md").write_text("# contrib", encoding="utf-8")
    (bundle / "CHANGELOG.md").write_text("# log", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)

    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]

    assert not any("/test/" in p for p in paths)
    for doc in ("README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        assert not any(p.endswith(f"capability-evolver/{doc}") for p in paths), doc
    # Kept: the skill's own entrypoint doc, its code, and Atlas's markdown.
    assert "workspace-uuid-atlas/skills/capability-evolver/SKILL.md" in paths
    assert "workspace-uuid-atlas/skills/capability-evolver/index.js" in paths
    assert "workspace-uuid-atlas/skills/capability-evolver/src/config.js" in paths
    assert "workspace-uuid-atlas/skills/capability-evolver/package.json" in paths
    assert "workspace-uuid-atlas/AGENTS.md" in paths
    assert "workspace-uuid-atlas/IDENTITY.md" in paths


def test_fingerprint_covers_exactly_what_staging_writes(tmp_path, monkeypatch):
    """The fingerprint and the staging loop share ``_is_staged``. If they ever
    diverged — fingerprint hashing a file the loop skips — every workspace
    would read as permanently stale and force-refresh the whole bundle on
    every provision. This pins the two to the same predicate."""
    root = tmp_path / "files"
    bundle = root / "skills" / "evolver"
    (bundle / "test").mkdir(parents=True)
    (root / "AGENTS.md").write_text("a", encoding="utf-8")
    (bundle / "SKILL.md").write_text("s", encoding="utf-8")
    (bundle / "README.md").write_text("r", encoding="utf-8")
    (bundle / "test" / "x.test.js").write_text("t", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)

    staged = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and atlas_seed._is_staged(p.relative_to(root))
    }
    assert staged == {"AGENTS.md", "skills/evolver/SKILL.md"}

    # Mutating a skipped file must not move the fingerprint; mutating a staged
    # one must. That is the property the shared predicate guarantees.
    before = atlas_seed._bundle_fingerprint()
    (bundle / "test" / "x.test.js").write_text("CHANGED", encoding="utf-8")
    (bundle / "README.md").write_text("CHANGED", encoding="utf-8")
    assert atlas_seed._bundle_fingerprint() == before
    (bundle / "SKILL.md").write_text("CHANGED", encoding="utf-8")
    assert atlas_seed._bundle_fingerprint() != before


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
    assert paths[0] == "workspace-uuid-atlas/.openclaw/isol8-setup-seed.json", f"state first, got: {paths}"


@pytest.mark.asyncio
async def test_state_file_timestamps_are_js_canonical(captured_ws, fake_files_dir):
    """Atlas's seeded setup-state sentinel timestamps must be JS-canonical
    (``YYYY-MM-DDTHH:MM:SS.mmmZ``) so OpenClaw 7.2's boot migration accepts them.
    Python's ``.isoformat()`` (6-digit micros + ``+00:00``) is rejected — the
    migration aborts and every agent run throws ``Legacy workspace setup state
    requires migration``."""
    import json as _json

    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=captured_ws)

    state = _json.loads(captured_ws.written["workspace-uuid-atlas/.openclaw/isol8-setup-seed.json"])
    _assert_js_canonical_roundtrip(state["bootstrapSeededAt"])
    _assert_js_canonical_roundtrip(state["setupCompletedAt"])


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
async def test_acp_section_appended_to_agents(captured_ws, tmp_path, monkeypatch):
    """Atlas's AGENTS.md ships the harness-delegation section appended to the
    vendored base copy."""
    _acp_files_dir(tmp_path, monkeypatch)
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]

    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=captured_ws)

    agents_md = captured_ws.written["workspace-uuid-atlas/AGENTS.md"]
    assert "base recruiter copy" in agents_md  # vendored base preserved
    assert "Coding-Harness Delegation (ACP)" in agents_md  # section appended
    assert 'sessions_spawn({ runtime: "acp" })' in agents_md


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
    where the default path would skip.

    Modeled as an EXISTING, already-migrated workspace (dir present, no stale
    state file, shared DB present) — force restage only ever targets existing
    containers, and the state marker is exempt from force (#1429)."""
    ws = MagicMock()
    wsdir = ws.agent_workspace_path.return_value
    wsdir.is_dir.return_value = True
    # (workspace_dir / ".openclaw" / "workspace-state.json").is_file() → False
    wsdir.__truediv__.return_value.__truediv__.return_value.is_file.return_value = False
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
    """force=True overwrites every staged BUNDLE file via write_file even when
    the file already exists (where write_file_if_missing would skip) — so an
    existing container picks up the rewritten bundle. write_file_if_missing is
    never used. The setup-state sentinel is EXEMPT: force targets an existing
    (booted, migrated) workspace whose sentinel the fork already consumed, and
    re-seeding setup state there is exactly the one-shot rule (#1429)."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=force_ws, force=True)
    # Force path NEVER touches the skip-if-exists writer.
    assert force_ws.if_missing_calls == []
    # IDENTITY.md exists (write_if_missing would return False) yet was overwritten.
    assert "workspace-uuid-atlas/IDENTITY.md" in force_ws.overwrote
    # The 3 bundle files (IDENTITY.md, build.lobster, skills/fast-io/SKILL.md)
    # force-written; the one-shot state marker is NOT -- checked for BOTH the
    # native sentinel we write today and the retired legacy name, so this stays
    # meaningful rather than vacuously passing on a renamed file.
    assert "workspace-uuid-atlas/.openclaw/isol8-setup-seed.json" not in force_ws.overwrote
    assert "workspace-uuid-atlas/.openclaw/workspace-state.json" not in force_ws.overwrote
    assert result["force"] is True
    assert result["reason"] is None
    # The fingerprint marker is stamped via write_file too, but it is
    # bookkeeping, not a bundle file — it must not count in `overwritten`.
    bundle_writes = [p for p in force_ws.overwrote if not p.endswith(".isol8-seed-fingerprint")]
    assert "workspace-uuid-atlas/.isol8-seed-fingerprint" in force_ws.overwrote
    assert result["overwritten"] == len(bundle_writes) == 3


@pytest.mark.asyncio
async def test_force_never_rewrites_workspace_state(force_ws, fake_files_dir):
    """The state marker is exempt from force in BOTH writers: no write_file, no
    write_file_if_missing, and (with no stale copy on disk) no delete either."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=force_ws, force=True)

    for state_rel in (
        "workspace-uuid-atlas/.openclaw/isol8-setup-seed.json",
        "workspace-uuid-atlas/.openclaw/workspace-state.json",
    ):
        assert state_rel not in force_ws.overwrote
        assert state_rel not in force_ws.if_missing_calls
    force_ws.delete_file.assert_not_called()


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
    """force=True: AGENTS.md is force-overwritten WITH the
    ACP section appended (force must carry the augmentation, not the vanilla
    copy). Also asserts written stays 0 under force (overwrites count in
    overwritten only, so atlas_seed.success doesn't fire on overwrites)."""
    _acp_files_dir(tmp_path, monkeypatch)
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
    bundle_writes = [p for p in force_ws.overwrote if not p.endswith(".isol8-seed-fingerprint")]
    assert result["overwritten"] == len(bundle_writes)


# ── Bundle-fingerprint auto-refresh ──────────────────────────────────────────
# write_file_if_missing means an existing workspace never picks up a rewritten
# bundle on its own; the fingerprint marker turns every pass through
# pre_stage_atlas into a staleness check that force-refreshes when the staged
# bundle no longer matches the repo bundle.


def _existing_ws(marker: str | None):
    """Stub for an EXISTING workspace (dir present, migrated) whose fingerprint
    marker reads ``marker`` — ``None`` models a pre-marker workspace where
    read_file raises (the legacy-container case)."""
    ws = MagicMock()
    wsdir = ws.agent_workspace_path.return_value
    wsdir.is_dir.return_value = True
    wsdir.__truediv__.return_value.__truediv__.return_value.is_file.return_value = False
    ws.overwrote = []
    ws.if_missing_calls = []

    def _write_file(user_id, path, content):
        ws.overwrote.append(path)

    def _write_if_missing(user_id, path, content):
        ws.if_missing_calls.append(path)
        return False  # every file already exists

    ws.write_file.side_effect = _write_file
    ws.write_file_if_missing.side_effect = _write_if_missing
    if marker is None:
        ws.read_file.side_effect = FileNotFoundError("no marker")
    else:
        ws.read_file.return_value = marker + "\n"
    return ws


_AGENTS = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
_MARKER_PATH = "workspace-uuid-atlas/.isol8-seed-fingerprint"


@pytest.mark.asyncio
async def test_stale_marker_auto_refreshes_existing_workspace(fake_files_dir):
    """A marker from an older bundle flips the pass to force-overwrite and
    restamps the marker with the current fingerprint."""
    ws = _existing_ws("0" * 64)
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=_AGENTS, workspace=ws)
    assert result["auto_refreshed"] is True
    assert "workspace-uuid-atlas/IDENTITY.md" in ws.overwrote
    assert _MARKER_PATH in ws.overwrote
    assert ws.if_missing_calls == []


@pytest.mark.asyncio
async def test_pre_marker_workspace_auto_refreshes(fake_files_dir):
    """A workspace staged before the marker existed (read_file raises) is
    treated as stale — this is the legacy-container case the refresh exists
    for (an Atlas missing the bridge-URL bundle files entirely)."""
    ws = _existing_ws(None)
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=_AGENTS, workspace=ws)
    assert result["auto_refreshed"] is True
    assert "workspace-uuid-atlas/build.lobster" in ws.overwrote


@pytest.mark.asyncio
async def test_current_marker_skips_refresh(fake_files_dir):
    """Marker matches the current bundle → plain missing-only pass, no bundle
    overwrites (only the marker restamp goes through write_file)."""
    from core.services.atlas_seed import _bundle_fingerprint

    ws = _existing_ws(_bundle_fingerprint())
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=_AGENTS, workspace=ws)
    assert result["auto_refreshed"] is False
    assert [p for p in ws.overwrote if not p.endswith(".isol8-seed-fingerprint")] == []
    assert "workspace-uuid-atlas/IDENTITY.md" in ws.if_missing_calls


@pytest.mark.asyncio
async def test_marker_not_stamped_when_a_file_errors(fake_files_dir):
    """A per-file write error must leave the marker unstamped so the next pass
    through write_user_configs retries the refresh instead of believing the
    workspace is current."""
    ws = _existing_ws("0" * 64)

    def _flaky(user_id, path, content):
        if path.endswith("IDENTITY.md"):
            raise OSError("EFS hiccup")
        ws.overwrote.append(path)

    ws.write_file.side_effect = _flaky
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=_AGENTS, workspace=ws)
    assert result["errored"] == 1
    assert _MARKER_PATH not in ws.overwrote


@pytest.mark.asyncio
async def test_fresh_workspace_stays_missing_only_and_stamps_marker(workspace_stub, fake_files_dir):
    """A fresh workspace (dir absent) never auto-refreshes — everything is new
    anyway — and records the bundle it received so later passes can compare."""
    result = await pre_stage_atlas(owner_id="user_x", paperclip_agents=_AGENTS, workspace=workspace_stub)
    assert result["auto_refreshed"] is False
    stamped = [c.args[1] for c in workspace_stub.write_file.call_args_list]
    assert _MARKER_PATH in stamped
