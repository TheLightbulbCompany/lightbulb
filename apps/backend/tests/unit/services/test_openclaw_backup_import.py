"""Tests for core.services.openclaw_backup_import — the normalizer that turns an
uploaded OpenClaw setup into the self-describing staging layout
``export_built_agent_from_staging`` consumes. Pure file/JSON work; every test
builds a real tar.gz and walks the module end to end."""

import io
import json
import os
import tarfile
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import openclaw_backup_import as bi  # noqa: E402

HOME = "/Users/alice"
STATE = f"{HOME}/.openclaw"
ROOT = "2026-08-22T04-58-46.581Z-openclaw-backup"
PAYLOAD_STATE = f"{ROOT}/payload/posix{STATE}"
SQLITE_HEADER = b"SQLite format 3\x00" + b"\x00" * 84


def _tar_from_tree(tree: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in tree.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _stream(data: bytes):
    return lambda: io.BytesIO(data)


def _config(agents_list: list[dict] | None = None, **extra) -> str:
    cfg: dict = {
        "agents": {"defaults": {"model": "anthropic/claude-opus-4-6", "workspace": "~/.openclaw/workspace"}},
        "mcp": {
            "servers": {"gh": {"command": "npx", "args": ["-y", "@mcp/github"], "env": {"GITHUB_TOKEN": "ghp_secret"}}}
        },
        "skills": {"entries": {"notes": {"enabled": True, "apiKey": "sk-live-123"}}},
        "plugins": {
            "entries": {
                "lobster": {"enabled": True},
                "weather": {"enabled": True, "config": {"apiKey": "w-secret", "units": "c"}},
            }
        },
        "tools": {"deny": ["browser"]},
        "meta": {"lastTouchedVersion": "2026.3.11"},
    }
    if agents_list is not None:
        cfg["agents"]["list"] = agents_list
    cfg.update(extra)
    return json.dumps(cfg)


def _installs_json() -> str:
    return json.dumps(
        {
            "version": 1,
            "plugins": [
                {
                    "pluginId": "anthropic",
                    "packageName": "@openclaw/anthropic-provider",
                    "packageVersion": "2026.5.28",
                    "origin": "bundled",
                    "rootDir": "/opt/openclaw/dist/extensions/anthropic",
                    "enabled": True,
                },
                {
                    "pluginId": "weather",
                    "packageName": "openclaw-weather",
                    "packageVersion": "1.2.0",
                    "origin": "npm",
                    "rootDir": f"{STATE}/extensions/weather",
                    "enabled": True,
                },
            ],
        }
    )


def backup_archive(*, agents_list=None, extra_tree=None, with_plugins=True, with_history=True) -> bytes:
    manifest = {
        "schemaVersion": 1,
        "archiveRoot": ROOT,
        "createdAt": "2026-08-22T04:58:46.581Z",
        "assets": [{"kind": "state", "sourcePath": STATE, "archivePath": PAYLOAD_STATE}],
    }
    tree: dict[str, bytes | str] = {
        f"{ROOT}/manifest.json": json.dumps(manifest),
        f"{PAYLOAD_STATE}/openclaw.json": _config(agents_list),
        f"{PAYLOAD_STATE}/workspace/AGENTS.md": "# AGENTS.md\n\nBe useful.\n",
        f"{PAYLOAD_STATE}/workspace/SOUL.md": "# SOUL.md\n",
        f"{PAYLOAD_STATE}/workspace/IDENTITY.md": "# IDENTITY.md\n\n- **Name:** Quill\n- **Vibe:** dry, precise\n",
        f"{PAYLOAD_STATE}/workspace/MEMORY.md": "# MEMORY\n",
        f"{PAYLOAD_STATE}/workspace/memory/2026-08-01.md": "learned things\n",
        f"{PAYLOAD_STATE}/skills/remotion/SKILL.md": "---\nname: remotion\n---\n# Remotion\n",
        f"{PAYLOAD_STATE}/cron/jobs.json": json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "j1",
                        "name": "Daily digest",
                        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                        "payload": {"kind": "message", "message": "digest"},
                    },
                    {
                        "id": "j2",
                        "agentId": "other",
                        "name": "Other agent job",
                        "schedule": {"kind": "cron", "expr": "0 1 * * *"},
                        "payload": {"kind": "message", "message": "x"},
                    },
                ],
            }
        ),
        # runtime junk that must never reach EFS
        f"{PAYLOAD_STATE}/state/openclaw.sqlite": SQLITE_HEADER,
        f"{PAYLOAD_STATE}/memory/main.sqlite": SQLITE_HEADER,
        f"{PAYLOAD_STATE}/credentials/oauth.json": '{"token": "nope"}',
        f"{PAYLOAD_STATE}/browser/openclaw/user-data/Default/Cache/data_0": b"\x00" * 4096,
        f"{PAYLOAD_STATE}/media/inbound/photo.jpg": b"\xff\xd8" * 100,
        f"{PAYLOAD_STATE}/completions/openclaw.zsh": "compdef\n",
        f"{PAYLOAD_STATE}/logs/gateway.log": "log\n",
        f"{PAYLOAD_STATE}/agents/main/agent/auth-profiles.json": "{}",
    }
    if with_history:
        tree[f"{PAYLOAD_STATE}/agents/main/agent/openclaw-agent.sqlite"] = SQLITE_HEADER
        tree[f"{PAYLOAD_STATE}/agents/main/sessions/sessions.json"] = json.dumps(
            {"agent:main:main": {"sessionId": "s1"}}
        )
        tree[f"{PAYLOAD_STATE}/agents/main/sessions/s1.jsonl"] = '{"type":"session","sessionKey":"agent:main:main"}\n'
    if with_plugins:
        tree[f"{PAYLOAD_STATE}/plugins/installs.json"] = _installs_json()
        tree[f"{PAYLOAD_STATE}/extensions/weather/package.json"] = json.dumps(
            {"name": "openclaw-weather", "version": "1.2.0", "dependencies": {"axios": "^1"}}
        )
        tree[f"{PAYLOAD_STATE}/extensions/weather/index.js"] = "module.exports = {}\n"
        tree[f"{PAYLOAD_STATE}/extensions/weather/node_modules/axios/index.js"] = "junk\n"
    tree.update(extra_tree or {})
    return _tar_from_tree(tree)


def state_dir_archive() -> bytes:
    return _tar_from_tree(
        {
            "openclaw.json": _config(),
            "workspace/AGENTS.md": "# AGENTS.md\n",
            "workspace/IDENTITY.md": "- **Name:** Quill\n",
            "skills/remotion/SKILL.md": "# Remotion\n",
            "cron/jobs.json": json.dumps({"version": 1, "jobs": []}),
        }
    )


def workspace_archive() -> bytes:
    return _tar_from_tree(
        {
            "workspace/AGENTS.md": "# AGENTS.md\n",
            "workspace/SOUL.md": "# SOUL.md\n",
            "workspace/IDENTITY.md": "- **Name:** Bare Bones\n",
        }
    )


def root_state_dir_archive() -> bytes:
    """R1: a ``tar -czf x.tgz -C ~/.openclaw …`` shape — the state dir IS the
    archive root (no wrapping directory), so its skip-relative prefix is ""."""
    return _tar_from_tree(
        {
            "openclaw.json": _config(),
            "workspace/AGENTS.md": "# AGENTS.md\n",
            "agents/main/agent/openclaw-agent.sqlite": SQLITE_HEADER,
            "browser/openclaw/user-data/x": b"\x00" * 16,
            "memory/main.sqlite": SQLITE_HEADER,
            "state/openclaw.sqlite": SQLITE_HEADER,
        }
    )


def artifact_shaped_archive() -> bytes:
    """R1: an Isol8 artifact re-upload — ``openclaw.json`` beside persona
    files at the root, no ``agents``/``state``/``credentials``/``browser``
    sibling. Workspace-shaped: NO state-relative skips apply, so its own
    ``memory/`` directory (agent content, not runtime junk) survives."""
    return _tar_from_tree(
        {
            "openclaw.json": _config(),
            "AGENTS.md": "# AGENTS.md\n",
            "memory/2026-08-01.md": "learned things\n",
        }
    )


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_backup_keeps_content_drops_runtime_junk(tmp_path: Path):
    raw = tmp_path / "raw"
    report = bi.extract_archive(_stream(backup_archive()), raw)
    st = raw / PAYLOAD_STATE
    assert (st / "workspace" / "AGENTS.md").is_file()
    assert (st / "skills" / "remotion" / "SKILL.md").is_file()
    assert (st / "cron" / "jobs.json").is_file()
    assert (st / "plugins" / "installs.json").is_file()
    assert (st / "extensions" / "weather" / "index.js").is_file()
    # history inputs survive (the only things kept under agents/)
    assert (st / "agents" / "main" / "agent" / "openclaw-agent.sqlite").is_file()
    assert (st / "agents" / "main" / "sessions" / "s1.jsonl").is_file()
    # runtime junk never lands
    for junk in ("state", "memory", "credentials", "browser", "media", "completions", "logs"):
        assert not (st / junk).exists(), junk
    assert not (st / "agents" / "main" / "agent" / "auth-profiles.json").exists()
    assert not (st / "extensions" / "weather" / "node_modules").exists()
    assert report.files > 0 and report.skipped_big == []


def test_extract_rejects_not_a_tarball(tmp_path: Path):
    with pytest.raises(bi.BackupImportError, match="tar.gz"):
        bi.extract_archive(_stream(b"this is not gzip"), tmp_path / "raw")


def test_extract_rejects_symlink_member(tmp_path: Path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(bi.BackupImportError, match="symlink"):
        bi.extract_archive(_stream(buf.getvalue()), tmp_path / "raw")


def test_extract_rejects_traversal(tmp_path: Path):
    data = _tar_from_tree({"../escape.txt": "x"})
    with pytest.raises(bi.BackupImportError, match="escapes"):
        bi.extract_archive(_stream(data), tmp_path / "raw")


def test_extract_caps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bi, "MAX_MEMBERS", 3)
    with pytest.raises(bi.BackupImportError, match="entries"):
        bi.extract_archive(_stream(_tar_from_tree({f"f{i}": "x" for i in range(5)})), tmp_path / "a")
    monkeypatch.setattr(bi, "MAX_MEMBERS", 200_000)
    monkeypatch.setattr(bi, "MAX_EXTRACTED_BYTES", 10)
    with pytest.raises(bi.BackupImportError, match="extraction cap"):
        bi.extract_archive(_stream(_tar_from_tree({"a": "x" * 11})), tmp_path / "b")


def test_extract_skips_oversize_single_file_with_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bi, "MAX_FILE_BYTES", 100)
    big = {f"{PAYLOAD_STATE}/agents/main/agent/openclaw-agent.sqlite": b"\x00" * 200}
    report = bi.extract_archive(_stream(backup_archive(extra_tree=big)), tmp_path / "raw")
    assert any(p.endswith("openclaw-agent.sqlite") for p in report.skipped_big)
    assert not (tmp_path / "raw" / PAYLOAD_STATE / "agents" / "main" / "agent" / "openclaw-agent.sqlite").exists()


def test_extract_root_state_dir_drops_runtime_keeps_content(tmp_path: Path):
    """R1(a)+(b): a root-level state dir (prefix "") matches the state-relative
    skip rules — browser/memory/state are dropped; workspace/ and the agent
    history sqlite survive."""
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(root_state_dir_archive()), raw)
    assert (raw / "workspace" / "AGENTS.md").is_file()
    assert (raw / "agents" / "main" / "agent" / "openclaw-agent.sqlite").is_file()
    for junk in ("browser", "memory", "state"):
        assert not (raw / junk).exists(), junk


def test_extract_artifact_shaped_archive_keeps_memory(tmp_path: Path):
    """R1(b): no agents/state/credentials/browser/workspace sibling next to
    the root openclaw.json means this is workspace-shaped, not a state dir —
    its own memory/ directory is agent content, not runtime junk."""
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(artifact_shaped_archive()), raw)
    assert (raw / "memory" / "2026-08-01.md").is_file()


# ---------------------------------------------------------------------------
# locate_source
# ---------------------------------------------------------------------------


def test_locate_backup_archive(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(backup_archive()), raw)
    src = bi.locate_source(raw)
    assert src.kind == "backup"
    assert src.state_dir == raw / PAYLOAD_STATE
    assert src.state_source_path == STATE
    assert src.config["mcp"]["servers"]["gh"]["command"] == "npx"
    assert raw / PAYLOAD_STATE / "workspace" in src.workspace_dirs


def test_locate_backup_manifest_asset_escape_rejected(tmp_path: Path):
    raw = tmp_path / "raw"
    manifest = {
        "schemaVersion": 1,
        "archiveRoot": ROOT,
        "createdAt": "x",
        "assets": [{"kind": "state", "sourcePath": STATE, "archivePath": f"{ROOT}/../../../etc"}],
    }
    bi.extract_archive(_stream(_tar_from_tree({f"{ROOT}/manifest.json": json.dumps(manifest)})), raw)
    with pytest.raises(bi.BackupImportError, match="escapes"):
        bi.locate_source(raw)


def test_locate_state_dir_archive(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(state_dir_archive()), raw)
    src = bi.locate_source(raw)
    assert src.kind == "state-dir" and src.state_dir == raw and src.workspace_dirs == [raw / "workspace"]


def test_locate_bare_workspace_one_level_down(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(workspace_archive()), raw)
    src = bi.locate_source(raw)
    assert src.kind == "workspace" and src.state_dir is None and src.config == {}
    assert src.workspace_dirs == [raw / "workspace"]


def test_locate_nothing_recognizable(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(_tar_from_tree({"README.txt": "hello"})), raw)
    with pytest.raises(bi.BackupImportError, match="no OpenClaw"):
        bi.locate_source(raw)


# ---------------------------------------------------------------------------
# list_agents / list_plugins
# ---------------------------------------------------------------------------


def test_list_agents_implicit_main_with_history(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(backup_archive()), raw)
    agents = bi.list_agents(bi.locate_source(raw))
    assert [a.id for a in agents] == ["main"]
    a = agents[0]
    assert a.name == "Quill"
    assert a.workspace_dir == raw / PAYLOAD_STATE / "workspace"
    assert "model" not in a.entry  # defaults are NOT folded in
    assert a.history is not None and a.history.sqlite is not None and a.history.sessions_dir is not None
    assert a.history.bytes > 0


def test_list_agents_no_history(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(backup_archive(with_history=False)), raw)
    assert bi.list_agents(bi.locate_source(raw))[0].history is None


def test_list_agents_explicit_list_with_per_agent_workspaces(tmp_path: Path):
    raw = tmp_path / "raw"
    archive = backup_archive(
        agents_list=[
            {"id": "main", "default": True, "name": "Main"},
            {"id": "work", "name": "Work Bot", "workspace": "~/.openclaw/workspace-work"},
        ],
        extra_tree={f"{PAYLOAD_STATE}/workspace-work/AGENTS.md": "# work\n"},
    )
    bi.extract_archive(_stream(archive), raw)
    by_id = {a.id: a for a in bi.list_agents(bi.locate_source(raw))}
    assert set(by_id) == {"main", "work"}
    assert by_id["main"].workspace_dir == raw / PAYLOAD_STATE / "workspace"
    assert by_id["work"].workspace_dir == raw / PAYLOAD_STATE / "workspace-work"
    assert by_id["work"].name == "Work Bot" and by_id["work"].history is None


def test_list_agents_bare_workspace_synthesizes_main(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(workspace_archive()), raw)
    agents = bi.list_agents(bi.locate_source(raw))
    assert len(agents) == 1 and agents[0].id == "main" and agents[0].name == "Bare Bones"


def test_list_plugins_skips_bundled_and_reads_deps(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(backup_archive()), raw)
    plugins = bi.list_plugins(bi.locate_source(raw))
    assert [p.id for p in plugins] == ["weather"]
    p = plugins[0]
    assert p.package_name == "openclaw-weather" and p.version == "1.2.0"
    assert p.source_dir == raw / PAYLOAD_STATE / "extensions" / "weather"
    assert p.has_dependencies is True
    # _scrub_secret_fields only scrubs an entry's OWN top-level secret-shaped
    # keys; "apiKey" here sits nested under "config", one level below the
    # entry, so it survives the scrub as-is (per catalog_slice.py semantics).
    assert p.config == {"enabled": True, "config": {"apiKey": "w-secret", "units": "c"}}


def test_list_plugins_none_without_installs(tmp_path: Path):
    raw = tmp_path / "raw"
    bi.extract_archive(_stream(backup_archive(with_plugins=False)), raw)
    assert bi.list_plugins(bi.locate_source(raw)) == []


# ---------------------------------------------------------------------------
# materialize / stage_upload / restage
# ---------------------------------------------------------------------------

FLEET = "2026.7.2"
ALL = bi.ImportSelection()


def test_stage_upload_backup_writes_self_describing_workspace(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    norm, report = bi.stage_upload(_stream(backup_archive()), staging, fleet_version=FLEET)
    assert norm.kind == "backup" and norm.agent_id == "main" and norm.name == "Quill"
    ws = staging / "workspace"
    assert (ws / "AGENTS.md").is_file() and (ws / "MEMORY.md").is_file()
    assert (ws / "memory" / "2026-08-01.md").is_file()
    assert (ws / "skills" / "remotion" / "SKILL.md").is_file()
    assert not (ws / "agents").exists() and not list(ws.rglob("*.sqlite"))
    assert (staging / "raw").is_dir()
    cfg = json.loads((ws / "openclaw.json").read_text())
    assert [a["id"] for a in cfg["agents"]["list"]] == ["main"]
    assert cfg["agents"]["list"][0]["name"] == "Quill"
    assert cfg["agents"]["list"][0]["description"] == "dry, precise"
    assert cfg["mcp"]["servers"]["gh"]["env"] == {"GITHUB_TOKEN": ""}
    assert cfg["skills"]["entries"]["notes"]["apiKey"] == ""
    assert cfg["tools"] == {"deny": ["browser"]}
    assert "meta" not in cfg and "channels" not in cfg
    assert norm.mcp_names == ["gh"] and sorted(norm.skill_names) == ["notes", "remotion"]
    assert [p.id for p in norm.plugins] == ["weather"]
    assert report.files > 0


def test_stage_upload_cron_default_agent_and_filtered(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    norm, _ = bi.stage_upload(_stream(backup_archive()), staging, fleet_version=FLEET)
    assert [j["name"] for j in norm.cron_jobs] == ["Daily digest"]
    sidecar = json.loads((staging / "cron-jobs.json").read_text())
    assert [j["name"] for j in sidecar] == ["Daily digest"] and "id" not in sidecar[0]


def test_restage_applies_selection(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    bi.stage_upload(_stream(backup_archive()), staging, fleet_version=FLEET)
    sel = bi.ImportSelection(skills={"notes"}, mcp_servers=set(), include_memory=False, include_cron=False)
    norm = bi.restage(staging, agent_id="main", selection=sel, fleet_version=FLEET)
    ws = staging / "workspace"
    assert not (ws / "skills" / "remotion").exists()  # workspace skill deselected
    assert not (ws / "memory").exists() and not (ws / "MEMORY.md").exists()
    cfg = json.loads((ws / "openclaw.json").read_text())
    assert cfg["mcp"] == {} and list(cfg["skills"]["entries"]) == ["notes"]
    assert norm.mcp_names == [] and norm.skill_names == ["notes"] and norm.cron_jobs == []
    assert json.loads((staging / "cron-jobs.json").read_text()) == []


def test_stage_upload_warnings(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    archive = backup_archive(
        agents_list=[
            {
                "id": "main",
                "default": True,
                "model": "anthropic/claude-opus-4-6",
                "archetype": "general",
                "channels": {"telegram": {}},
            },
            {"id": "work", "name": "Work Bot", "workspace": "~/.openclaw/workspace-work"},
        ],
        extra_tree={f"{PAYLOAD_STATE}/workspace-work/AGENTS.md": "# work\n"},
    )
    norm, _ = bi.stage_upload(_stream(archive), staging, fleet_version=FLEET)
    joined = "\n".join(norm.warnings)
    assert "2026.3.11" in joined and FLEET in joined
    assert "archetype" in joined and "channel" in joined.lower() and "model" in joined.lower()
    manifest = json.loads((staging / "import-manifest.json").read_text())
    assert [a["id"] for a in manifest["agents"]] == ["main", "work"] and manifest["agent_id"] == "main"


def test_stage_upload_warns_on_local_mcp_paths(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    cfg_tree = {
        f"{PAYLOAD_STATE}/openclaw.json": _config(
            mcp={"servers": {"fs": {"command": "node", "args": ["/Users/alice/mcp/fs.js"]}}}
        )
    }
    norm, _ = bi.stage_upload(_stream(backup_archive(extra_tree=cfg_tree)), staging, fleet_version=FLEET)
    assert any("'fs'" in w and "/Users/alice/mcp/fs.js" in w for w in norm.warnings)


def test_restage_other_agent_and_unknown(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    archive = backup_archive(
        agents_list=[
            {"id": "main", "default": True},
            {"id": "work", "name": "Work Bot", "workspace": "~/.openclaw/workspace-work"},
        ],
        extra_tree={f"{PAYLOAD_STATE}/workspace-work/AGENTS.md": "# work only\n"},
    )
    bi.stage_upload(_stream(archive), staging, fleet_version=FLEET)
    norm = bi.restage(staging, agent_id="work", selection=ALL, fleet_version=FLEET)
    assert norm.agent_id == "work"
    assert (staging / "workspace" / "AGENTS.md").read_text() == "# work only\n"
    assert not (staging / "workspace" / "SOUL.md").exists()
    with pytest.raises(bi.BackupImportError, match="not in this backup"):
        bi.restage(staging, agent_id="nope", selection=ALL, fleet_version=FLEET)


def test_stage_upload_state_dir_and_bare_workspace(tmp_path: Path):
    s1 = tmp_path / "workspace-import-aaaaaaaaaaaa"
    norm, _ = bi.stage_upload(_stream(state_dir_archive()), s1, fleet_version=FLEET)
    assert norm.kind == "state-dir" and (s1 / "workspace" / "skills" / "remotion" / "SKILL.md").is_file()
    s2 = tmp_path / "workspace-import-bbbbbbbbbbbb"
    norm, _ = bi.stage_upload(_stream(workspace_archive()), s2, fleet_version=FLEET)
    assert norm.kind == "workspace" and norm.name == "Bare Bones" and norm.plugins == []
    cfg = json.loads((s2 / "workspace" / "openclaw.json").read_text())
    assert cfg["agents"]["list"] == [{"id": "main", "name": "Bare Bones"}]
    assert cfg["mcp"] == {} and cfg["skills"] == {} and cfg["plugins"] == {} and cfg["tools"] == {}


def test_stage_upload_agent_without_workspace_warns(tmp_path: Path):
    staging = tmp_path / "workspace-import-abcdef123456"
    archive = backup_archive(
        agents_list=[{"id": "ghost", "name": "Ghost"}],
        extra_tree={f"{PAYLOAD_STATE}/workspace-other/AGENTS.md": "# other\n"},
    )
    norm, _ = bi.stage_upload(_stream(archive), staging, fleet_version=FLEET)
    assert any("no workspace directory" in w for w in norm.warnings)
    assert (staging / "workspace" / "openclaw.json").is_file()


def test_stage_upload_state_level_extras_ship_as_workspace_content(tmp_path: Path):
    """R2: an Isol8 artifact re-upload (openclaw.json beside persona files,
    the state dir IS the workspace) keeps memory/ and tools/ as ordinary
    workspace content — they are NOT in _STATE_RUNTIME_DIRS, unlike a real
    state dir where memory/*.sqlite is already dropped at extraction time."""
    staging = tmp_path / "workspace-import-cccccccccccc"
    archive = _tar_from_tree(
        {
            "openclaw.json": _config(),
            "AGENTS.md": "# a\n",
            "IDENTITY.md": "- **Name:** Re\n",
            "memory/2026-08-01.md": "x\n",
            "tools/helper.sh": "#!/bin/sh\n",
        }
    )
    norm, _ = bi.stage_upload(_stream(archive), staging, fleet_version=FLEET)
    ws = staging / "workspace"
    assert (ws / "memory" / "2026-08-01.md").is_file()
    assert (ws / "tools" / "helper.sh").is_file()
    cfg = json.loads((ws / "openclaw.json").read_text())
    assert cfg["agents"]["list"] == [{"id": "main", "name": "Re"}]
    assert cfg["mcp"]["servers"]["gh"]["env"] == {"GITHUB_TOKEN": ""}
    assert cfg["skills"]["entries"]["notes"]["apiKey"] == ""
    assert cfg["tools"] == {"deny": ["browser"]}
