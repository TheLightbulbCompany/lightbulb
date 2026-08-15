"""Tests for agent_export — Path B (snapshot agent from EFS)."""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import agent_export  # noqa: E402


@pytest.fixture
def fake_efs(tmp_path: Path):
    """Builds /tmp/<root>/users/<seller>/workspace-<agent-id>/ with sample
    files and patches get_workspace() to return a Workspace anchored at
    <root>.

    Layout mirrors the real OpenClaw EFS convention:
      agents/{id}/      → OpenClaw internal state (sessions/, agent/auth)
      workspace-{id}/  → seller-facing content (persona files, skills,
                          per-agent openclaw.json) — what gets exported.

    Also writes the seller's per-user openclaw.json with an agents.list[]
    entry for ``agent_id`` so ``export_agent_from_efs`` can synthesize a
    real openclaw_slice (matching the admin-catalog publish behavior).
    """
    seller_id = "user_seller_abc"
    agent_id = "my-agent-001"

    user_root = tmp_path / "users" / seller_id
    agent_dir = user_root / f"workspace-{agent_id}"
    agent_dir.mkdir(parents=True)

    # Per-user openclaw.json — what the slice extraction reads.
    (user_root / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {
                            "id": agent_id,
                            "name": "MyAgent",
                            "identity": {"name": "MyAgent", "emoji": "🤖"},
                            "skills": ["search", "code-review"],
                            "model": "anthropic.claude-3-5-sonnet",
                            "channels": {"telegram": {"chatId": "123"}},
                            "workspace": ".openclaw/workspace-old-id",
                        },
                    ],
                },
                "plugins": {"entries": {"memory": {"enabled": True}}},
                "tools": {"profile": "default", "allow": ["bash", "read"]},
            }
        ),
        encoding="utf-8",
    )

    # Per-agent openclaw.json — used only by _summarize_openclaw_config for
    # the manifest's display name + description.
    (agent_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "identity": {"name": "MyAgent", "vibe": "helpful", "description": "An agent"},
                "tools": {"foo": {}, "bar": {}},
            }
        ),
        encoding="utf-8",
    )
    (agent_dir / "README.md").write_text("# Hello\n", encoding="utf-8")
    (agent_dir / "scripts").mkdir()
    (agent_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    # Junk dirs that should be skipped.
    (agent_dir / "__pycache__").mkdir()
    (agent_dir / "__pycache__" / "ignored.pyc").write_bytes(b"junk")
    (agent_dir / ".git").mkdir()
    (agent_dir / ".git" / "HEAD").write_bytes(b"junk")

    # Patch get_workspace() to anchor at tmp_path/users (matches the
    # production EFS_MOUNT_PATH convention which already ends in /users).
    from core.containers import workspace as workspace_module

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        yield seller_id, agent_id, agent_dir


def _list_tar_names(tar_bytes: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def test_export_happy_path(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    assert pkg.format == "openclaw"
    assert pkg.manifest["name"] == "MyAgent"
    assert pkg.manifest["agent_id"] == agent_id
    names = _list_tar_names(pkg.tarball_bytes)
    assert any("openclaw.json" in n for n in names)
    assert any("README.md" in n for n in names)
    assert any("scripts/run.sh" in n for n in names)


def test_export_manifest_carries_requirements(fake_efs):
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "pipelines").mkdir()
    (agent_dir / "pipelines" / "daily-feed.lobster").write_text(
        "\n".join(
            [
                "--tool slack",
                "--tool summarize",
                "curl https://api.stripe.com/v1/charges",
                "curl https://production.plaid.com/x -d $PLAID_SECRET",
            ]
        ),
        encoding="utf-8",
    )
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    keys = {r["key"]: r["disposition"] for r in pkg.manifest["requirements"]}
    assert keys["summarize"] == "included"
    assert keys["slack"] == "connect"
    assert keys["stripe"] == "connect"  # host-only, no env -> connectable
    assert keys["plaid"] == "byo"
    assert pkg.manifest["scan_incomplete"] is False

    creds = pkg.manifest["requires"]["credentials"]
    provs = {c["provider"] for c in creds}
    assert provs == {"stripe"}  # registry connect only; channel + byo + bundled excluded
    # Scanned connect deps are advisory — the scanner over-reports, so a
    # detected connect must never hard-block a hire (only slice-bundled MCP
    # providers do). assert_credentials_satisfied skips optional entries.
    assert all(c["optional"] is True for c in creds)


def test_preview_includes_requirements(fake_efs):
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "pipelines").mkdir()
    (agent_dir / "pipelines" / "p.lobster").write_text(
        "--tool summarize\ncurl https://production.plaid.com/x $PLAID_SECRET\n",
        encoding="utf-8",
    )
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    assert "requirements" in preview
    keys = {r["key"]: r["disposition"] for r in preview["requirements"]}
    assert keys["plaid"] == "byo"
    assert keys["summarize"] == "included"


def test_export_skips_junk_dirs(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any("__pycache__" in n for n in names)
    assert not any(".git" in n for n in names)


def test_export_skips_boot_and_workspace_plugin_files(fake_efs):
    """``BOOT.md`` triggers an autonomous LLM run on every gateway restart
    (internal hooks are enabled fleet-wide) and ``openclaw.plugin.json`` makes
    a workspace dir loadable in-process code — both are seller-authored
    ingestion vectors into a buyer's container, so neither may ship, at any
    depth. ``BOOTSTRAP.md`` is a legitimate persona file and must survive.
    """
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "BOOT.md").write_text("boot instructions\n", encoding="utf-8")
    (agent_dir / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (agent_dir / "BOOTSTRAP.md").write_text("first-run persona\n", encoding="utf-8")
    nested = agent_dir / "nested"
    nested.mkdir()
    (nested / "BOOT.md").write_text("nested boot\n", encoding="utf-8")
    (nested / "openclaw.plugin.json").write_text("{}", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any(n.rsplit("/", 1)[-1] == "BOOT.md" for n in names), names
    assert not any(n.rsplit("/", 1)[-1] == "openclaw.plugin.json" for n in names), names
    assert any(n.endswith("BOOTSTRAP.md") for n in names), names
    assert any("README.md" in n for n in names), names


def test_export_skips_uploads_dir(fake_efs):
    """``uploads/`` is OpenClaw's per-workspace attachment/media staging dir —
    runtime user data, not part of the agent's portable definition. It must
    never ship in a marketplace artifact: doing so leaks the seller's users'
    uploaded attachments and drags along stale snapshots of files that live
    canonically in pipelines/scripts. The whole dir is stripped regardless of
    contents (unlike the ambiguous ``sessions/``/``agent/`` workspace names,
    which DO ship — see the regression test above).
    """
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "uploads").mkdir()
    (agent_dir / "uploads" / "user-photo.png").write_bytes(b"\x89PNG\r\n")
    (agent_dir / "uploads" / "stale-bundle.lobster").write_text(
        "curl https://api.perplexity.ai/chat/completions -d @-", encoding="utf-8"
    )
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any("uploads/" in n for n in names), names
    # Sanity: legitimate workspace content still ships.
    assert any("README.md" in n for n in names), names


def test_export_requires_acp_defaults_false(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest["requires_acp"] is False


def test_export_requires_acp_when_declared(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id, enable_acp=True)
    assert pkg.manifest["requires_acp"] is True


def test_export_requires_desktop_defaults_false(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest["requires_desktop"] is False


def test_export_requires_desktop_when_publisher_declares(fake_efs):
    """'Needs the desktop app' is a publisher-set flag (not scanned) — recorded
    on the manifest for buyer-facing disclosure."""
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id, requires_desktop=True)
    assert pkg.manifest["requires_desktop"] is True


def test_export_ships_workspace_subdirs_named_sessions_or_agent(fake_efs):
    """Regression: a guard that strips any top-level ``sessions/`` or
    ``agent/`` dir from the tarball is unnecessary now that exports come
    from ``workspace-{id}/`` (runtime state lives in the sibling
    ``agents/{id}/`` tree we don't touch) and would silently drop
    legitimate seller content in workspaces named ``sessions/`` (e.g.
    user-recorded data) or ``agent/`` (e.g. project files describing the
    agent).
    """
    seller_id, agent_id, agent_dir = fake_efs
    # Plant content under top-level "sessions/" and "agent/" subdirs of
    # the workspace. These names happen to collide with OpenClaw's
    # internal-state dirs, but they sit inside the WORKSPACE root — the
    # export must ship them.
    (agent_dir / "sessions").mkdir()
    (agent_dir / "sessions" / "interview-notes.md").write_text("# Interview notes\nKey themes...", encoding="utf-8")
    (agent_dir / "agent").mkdir()
    (agent_dir / "agent" / "ARCHITECTURE.md").write_text("# How this agent works", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    assert any("sessions/interview-notes.md" in n for n in names), names
    assert any("agent/ARCHITECTURE.md" in n for n in names), names


def test_credential_filter_blocks_txt_pasted_secrets(fake_efs):
    """Sellers commonly paste credentials into a .txt file
    (``secret.txt``, ``credentials.txt``, ``private-key.txt``). The
    content scan only inspects persona/.md/openclaw.json, so these slip
    through if .txt isn't a config-shape extension. Add .txt to the gate.
    """
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "secret.txt").write_text("aws_secret_key=hunter2\n", encoding="utf-8")
    (agent_dir / "credentials.txt").write_text("API_KEY=...", encoding="utf-8")
    (agent_dir / "private-key.txt").write_text("-----BEGIN-----", encoding="utf-8")
    # Non-credential .txt file still ships.
    (agent_dir / "notes.txt").write_text("# brainstorm", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    for forbidden in ("secret.txt", "credentials.txt", "private-key.txt"):
        assert not any(forbidden in n for n in names), f"{forbidden!r} leaked through the credential filter: {names}"
    assert any("notes.txt" in n for n in names), names


def test_credential_filter_blocks_extensionless_aws_credentials(fake_efs):
    """The AWS-style ``credentials`` file (extensionless, e.g. copied from
    ``~/.aws/credentials``) must be blocked. A substring-based filter
    catches it; a tighter word-boundary filter with an early-return on
    extensionless files bypasses the token check.

    Also pins that an extensionless ``credentials-helper`` script does
    NOT get blocked — only the EXACT canonical credential filenames hit
    the extensionless branch.
    """
    seller_id, agent_id, agent_dir = fake_efs
    # AWS-style: literal `credentials` filename, no extension, contents
    # look like AWS credentials file.
    (agent_dir / "credentials").write_text(
        "[default]\naws_access_key_id=AKIAEXAMPLE\naws_secret_access_key=hunter2\n",
        encoding="utf-8",
    )
    (agent_dir / "secrets").write_text("BIG_SECRET=hunter2\n", encoding="utf-8")
    # Same shape, but a script. Should ship — the seller might have a
    # ``credentials-helper`` CLI shim that's just code.
    (agent_dir / "credentials-helper").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    # Extensionless credential filenames are blocked.
    assert not any(n.endswith("/credentials") or n == "./credentials" for n in names), names
    assert not any(n.endswith("/secrets") or n == "./secrets" for n in names), names
    # Extensionless helper script still ships.
    assert any("credentials-helper" in n for n in names), names


def test_credential_filter_word_boundary_does_not_strip_skills(fake_efs):
    """A naive substring-match on the basename drops a skill named
    ``secretary.md`` or ``secret-santa.md`` or a helper file
    ``credentials-helper.ts`` — yet ``_enumerate_workspace_skill_names``
    still classifies the skill as WORKSPACE bucket, leaving the buyer
    with a config slice referencing a file that wasn't actually shipped.

    Whole-token + extension-shape match keeps these legit files in.
    """
    seller_id, agent_id, agent_dir = fake_efs
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "secretary.md").write_text("# Secretary skill", encoding="utf-8")
    (skills_dir / "secret-santa.md").write_text("# Secret Santa skill", encoding="utf-8")
    (skills_dir / "credentials-helper.ts").write_text("export const x = 1;", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    assert any("skills/secretary.md" in n for n in names), names
    assert any("skills/secret-santa.md" in n for n in names), names
    assert any("skills/credentials-helper.ts" in n for n in names), names


def test_export_skips_dotfiles_anywhere_in_tree(fake_efs, tmp_path):
    """Regression: hidden files / hidden dirs at any depth must NOT ship in
    the marketplace artifact. Sellers commonly have .env, .ssh/*, .aws/
    credentials, .openclaw/secrets in their workspace; bundling them
    would deliver secrets directly to buyers.
    """
    seller_id, agent_id, agent_dir = fake_efs
    # Plant a bunch of dotfile / dotdir scenarios.
    (agent_dir / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    (agent_dir / ".ssh").mkdir()
    (agent_dir / ".ssh" / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    # Hidden file nested inside an otherwise-allowed directory.
    (agent_dir / "scripts" / ".secret").write_text("password=hunter2\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    # None of the dotfile paths show up.
    assert not any(".env" in n.split("/")[-1] for n in names if n != "."), names
    assert not any(".ssh" in n for n in names), names
    assert not any(".secret" in n for n in names), names


def test_export_skips_backup_and_temp_files(fake_efs):
    """Regression: editing backups (*.bak, *.bak.<ts>, *.orig) and temp files
    (*.tmp, *~) ride along from in-place workspace edits but are inert cruft —
    they must never reach a buyer's installed workspace."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "AGENTS.md.bak.20260624050826").write_text("old\n", encoding="utf-8")
    (agent_dir / "BOOTSTRAP.md.bak").write_text("old\n", encoding="utf-8")
    (agent_dir / "draft.tmp").write_text("tmp\n", encoding="utf-8")
    (agent_dir / "merge.orig").write_text("orig\n", encoding="utf-8")
    (agent_dir / "notes~").write_text("editor backup\n", encoding="utf-8")
    (agent_dir / "pipelines").mkdir(exist_ok=True)
    (agent_dir / "pipelines" / "x.lobster.bak.20260624032047").write_text("old\n", encoding="utf-8")
    # Hyphen-separated backup (skills-install leftover) — the variant that
    # slipped a dot-only ".bak." check.
    (agent_dir / "openclaw.json.bak-20260620-0652-skills").write_text("old\n", encoding="utf-8")
    # Sanity: legit filenames that merely contain "backup" (no ".bak") still ship.
    (agent_dir / "backup-notes.md").write_text("kept\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any(".bak" in n for n in names), names
    assert not any(n.endswith(".tmp") for n in names), names
    assert not any(n.endswith(".orig") for n in names), names
    assert not any(n.endswith("~") for n in names), names
    assert any(n.endswith("backup-notes.md") for n in names), names


def test_export_invalid_agent_id_format(fake_efs):
    seller_id, _, _ = fake_efs
    with pytest.raises(agent_export.InvalidAgentIdError):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="../escape")


def test_export_path_traversal_attempt_rejected(fake_efs):
    seller_id, _, _ = fake_efs
    # The regex covers basic ../ — but also ensure resolved-path check
    # rejects clever encodings. agent_id with embedded slash should fail
    # the regex first.
    with pytest.raises(agent_export.InvalidAgentIdError):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="abc/def")


def test_export_missing_agent_dir_raises_not_found(fake_efs):
    seller_id, _, _ = fake_efs
    with pytest.raises(agent_export.AgentNotFoundError):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="never-existed")


def test_export_snapshot_is_deterministic(fake_efs):
    seller_id, agent_id, _ = fake_efs
    a = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    b = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    # Identical content → identical bytes (mtimes/uids zeroed).
    assert a.tarball_bytes == b.tarball_bytes


def test_export_invalid_seller_id_rejected(tmp_path):
    with pytest.raises(agent_export.InvalidAgentIdError):
        # WorkspaceError wraps to InvalidAgentIdError.
        agent_export.export_agent_from_efs(seller_id="../bad", agent_id="x")


def test_export_reads_from_workspaces_dir_not_agents_dir(tmp_path: Path):
    """Regression for the agents/-vs-workspace-dirs mix-up: the OpenClaw EFS
    layout puts INTERNAL runtime state under ``agents/{id}/`` and the
    actual seller workspace content under
    ``workspace-{id}/``. The export must tar the workspace dir; the agents
    dir holds only sessions/ logs and OAuth tokens.

    Plant identical filenames in both dirs and confirm only the workspace-dir
    copy makes it into the tarball.
    """
    seller_id = "user_seller_xyz"
    agent_id = "test-agent-001"
    user_root = tmp_path / "users" / seller_id
    user_root.mkdir(parents=True)
    (user_root / "openclaw.json").write_text(
        json.dumps({"agents": {"list": [{"id": agent_id, "name": "T", "skills": []}]}}),
        encoding="utf-8",
    )
    # Plant a marker in the AGENTS dir — this should be ignored.
    agents_dir = user_root / "agents" / agent_id
    agents_dir.mkdir(parents=True)
    (agents_dir / "MARKER.md").write_text("agents-dir-marker (must NOT ship)", encoding="utf-8")
    # And a different marker in the WORKSPACES dir — this is what ships.
    workspaces_dir = user_root / f"workspace-{agent_id}"
    workspaces_dir.mkdir(parents=True)
    (workspaces_dir / "IDENTITY.md").write_text("workspace content (must ship)", encoding="utf-8")

    from core.containers import workspace as workspace_module

    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert any("IDENTITY.md" in n for n in names), names
    assert not any("MARKER.md" in n for n in names), f"agents/-dir content leaked into the tarball: {names}"


def test_export_skips_credential_shaped_filenames(fake_efs):
    """Defense in depth on top of marketplace_safety's content scan: files
    whose names look like credentials (``resy-credentials.json``,
    ``aws.credentials.json``, ``*.secret.json``, ``id_rsa``, etc.) are
    skipped at tar time so the seller's account doesn't ship to every
    buyer if the scan ever misses the content pattern.
    """
    seller_id, agent_id, agent_dir = fake_efs
    # Plant credential-shaped filenames the seller might have left in
    # their workspace.
    (agent_dir / "resy-credentials.json").write_text(
        '{"email":"seller@example.com","password":"hunter2"}',
        encoding="utf-8",
    )
    (agent_dir / "aws.credentials.json").write_text("{}", encoding="utf-8")
    (agent_dir / "openai.secret.json").write_text("{}", encoding="utf-8")
    (agent_dir / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    (agent_dir / "service-account-secrets.yaml").write_text("token: x", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    for forbidden in (
        "resy-credentials.json",
        "aws.credentials.json",
        "openai.secret.json",
        "id_rsa",
        "service-account-secrets.yaml",
    ):
        assert not any(forbidden in n for n in names), (
            f"credential-shaped file {forbidden!r} leaked into the tarball: {names}"
        )


def test_export_synthesizes_slice_from_seller_config(fake_efs):
    """The CatalogPackage's openclaw_slice carries the seller's agent entry
    with user-specific fields stripped (model, channels, workspace, id) and
    the publisher's plugins + tools blocks intact. This is what makes the
    buyer-side deploy auto-register the new agent in their openclaw.json.
    """
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)

    slice_ = pkg.openclaw_slice
    assert slice_, "openclaw_slice must be populated, not empty"

    agent = slice_["agent"]
    # Stripped fields (user/tier-specific):
    assert "model" not in agent, "model must be stripped from slice"
    assert "channels" not in agent, "channels must be stripped from slice"
    assert "workspace" not in agent, "workspace must be stripped from slice"
    assert "id" not in agent, "id must be stripped (regenerated per-deploy)"
    # Carried fields:
    assert agent.get("name") == "MyAgent"
    assert agent.get("skills") == ["search", "code-review"]
    assert agent.get("identity", {}).get("emoji") == "🤖"

    # Plugins + tools blocks travel verbatim.
    assert slice_["plugins"] == {"entries": {"memory": {"enabled": True}}}
    assert slice_["tools"] == {"profile": "default", "allow": ["bash", "read"]}

    # cron_jobs key always present (empty list when seller has none).
    assert slice_["cron_jobs"] == []


def test_export_replaces_seller_agent_id_in_mcp_args_with_sentinel(fake_efs, tmp_path: Path):
    """Regression for marketplace-deploy-symmetry (spec 2026-05-18): the
    slice's mcp.servers.X.args paths reference the seller's workspace,
    e.g. ``/home/node/.openclaw/workspace-{seller_agent_id}/mcp-resy/index.js``.
    On deploy that path doesn't exist in the buyer's container. Replace
    the seller-keyed segment with __AGENT_DIR__ at export so deploy can
    substitute the buyer's new agent_id cleanly.
    """
    seller_id, agent_id, agent_dir = fake_efs
    # Seller's openclaw.json already has an mcp servers entry — make it
    # carry an args path that points at the seller's workspaces dir.
    cfg_path = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {
        "servers": {
            "resy": {
                "command": "node",
                "args": [f"/home/node/.openclaw/workspace-{agent_id}/mcp-resy/index.js"],
                "env": {"RESY_EMAIL": "", "RESY_PASSWORD": ""},
            }
        }
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(
        seller_id=seller_id,
        agent_id=agent_id,
        selected_mcps=["resy"],
    )

    args = pkg.openclaw_slice["mcp"]["servers"]["resy"]["args"]
    # Seller's agent_id MUST NOT appear in the slice — it's
    # buyer-non-portable.
    assert agent_id not in args[0], f"seller agent_id leaked into slice mcp args: {args}"
    # Sentinel must be substituted in.
    assert "__AGENT_DIR__" in args[0], f"__AGENT_DIR__ sentinel missing from slice mcp args: {args}"
    # Rest of the path (mcp-resy/index.js) preserved.
    assert args[0].endswith("/mcp-resy/index.js"), args[0]


def test_export_normalizes_legacy_workspaces_layout_in_mcp_args(fake_efs, tmp_path: Path):
    """Sellers whose containers predate the workspace-layout migration still
    carry the retired ``workspaces/{agent_id}`` spelling in MCP args. Export
    must normalize that OLD form to the same ``workspace-__AGENT_DIR__``
    sentinel the new form produces, so every artifact ships layout-current.
    """
    seller_id, agent_id, agent_dir = fake_efs
    cfg_path = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {
        "servers": {
            "resy": {
                "command": "node",
                "args": [f"/home/node/.openclaw/workspaces/{agent_id}/mcp-resy/index.js"],
                "env": {"RESY_EMAIL": ""},
            }
        }
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(
        seller_id=seller_id,
        agent_id=agent_id,
        selected_mcps=["resy"],
    )

    args = pkg.openclaw_slice["mcp"]["servers"]["resy"]["args"]
    assert agent_id not in args[0], f"seller agent_id leaked into slice mcp args: {args}"
    # Old plural-dir spelling must be gone; the modern sentinel form remains.
    assert "workspaces/" not in args[0], args[0]
    assert "workspace-__AGENT_DIR__" in args[0], args[0]
    assert args[0].endswith("/mcp-resy/index.js"), args[0]


def test_export_carries_seller_cron_jobs_targeting_this_agent(fake_efs, tmp_path):
    """Cron jobs that target this agent get carried into the slice with
    runtime fields (id, sessionKey, state, createdAtMs, updatedAtMs)
    stripped — they're regenerated at deploy time.
    """
    seller_id, agent_id, _ = fake_efs
    cron_dir = tmp_path / "users" / seller_id / "cron"
    cron_dir.mkdir()
    (cron_dir / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "stale-uuid-to-strip",
                        "agentId": agent_id,
                        "sessionKey": "agent:old:user_seller_abc",
                        "state": {"nextRunAtMs": 999},
                        "createdAtMs": 111,
                        "updatedAtMs": 222,
                        "name": "morning standup",
                        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "UTC"},
                        "delivery": {"channel": "telegram", "mode": "message"},
                    },
                    {
                        "id": "different-agent-job",
                        "agentId": "some-other-agent",
                        "name": "irrelevant",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    cron = pkg.openclaw_slice["cron_jobs"]
    assert len(cron) == 1, "only this agent's cron jobs should ride along"
    job = cron[0]
    # Runtime fields stripped; non-runtime fields preserved.
    for stripped in ("id", "sessionKey", "state", "createdAtMs", "updatedAtMs"):
        assert stripped not in job, f"{stripped} must be stripped at slice time"
    assert job.get("name") == "morning standup"
    assert job.get("agentId") == agent_id


def test_export_missing_user_config_raises(fake_efs, tmp_path):
    """If the seller has no per-user openclaw.json, publish must fail with a
    clear error rather than shipping an empty slice.
    """
    seller_id, agent_id, _ = fake_efs
    user_config = tmp_path / "users" / seller_id / "openclaw.json"
    user_config.unlink()
    with pytest.raises(agent_export.AgentNotInConfigError, match="no openclaw.json"):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)


def test_export_agent_not_in_config_raises(fake_efs, tmp_path):
    """If the agent dir exists but the agent_id isn't in the seller's
    openclaw.json agents.list, fail loudly — we can't synthesize a slice
    without the agent's identity/skills/tools.
    """
    seller_id, _, _ = fake_efs
    # Plant a workspace dir for an agent that isn't in the user-level config.
    orphan_dir = tmp_path / "users" / seller_id / "workspace-orphan-agent-99"
    orphan_dir.mkdir()
    (orphan_dir / "README.md").write_text("orphan", encoding="utf-8")
    with pytest.raises(agent_export.AgentNotInConfigError, match="not registered"):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="orphan-agent-99")


# ---------------------------------------------------------------------------
# is_reserved_friday_agent — Friday-publish guard
# ---------------------------------------------------------------------------


class _StubFetch:
    """Async-callable stub for the Paperclip agents fetcher."""

    def __init__(self, agents):
        self.agents = agents
        self.calls = 0

    async def __call__(self, *, owner_id):
        self.calls += 1
        return self.agents


@pytest.mark.asyncio
async def test_is_reserved_friday_agent_true_when_id_matches_ceo():
    fetch = _StubFetch(
        [
            {"id": "friday-uuid-001", "role": "ceo", "name": "Friday"},
            {"id": "other-agent", "role": "engineer"},
        ]
    )
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="friday-uuid-001",
        fetch_paperclip_agents=fetch,
    )
    assert result is True


@pytest.mark.asyncio
async def test_is_reserved_friday_agent_false_for_non_ceo_agent():
    fetch = _StubFetch(
        [
            {"id": "friday-uuid-001", "role": "ceo"},
            {"id": "my-custom-agent", "role": "engineer"},
        ]
    )
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="my-custom-agent",
        fetch_paperclip_agents=fetch,
    )
    assert result is False


@pytest.mark.asyncio
async def test_is_reserved_friday_agent_false_when_paperclip_genuinely_empty():
    """A GENUINELY empty agent list (provisioning hasn't run) still allows —
    there is no Friday to protect yet. Distinct from a fetch FAILURE, which
    raises and blocks (see the outage test below)."""
    fetch = _StubFetch([])
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="my-custom-agent",
        fetch_paperclip_agents=fetch,
    )
    assert result is False


@pytest.mark.asyncio
async def test_friday_guard_blocks_export_when_paperclip_fetch_fails(monkeypatch):
    """Fail CLOSED on an outage: the default fetcher's failure used to return
    [], which the guard read as "allow" — a Paperclip outage let a seller
    export Friday (owner-keyed identity/memory into an immutable artifact).
    The typed error propagates so the router can 503 with retry advice."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("core.config.settings.PAPERCLIP_INTERNAL_URL", "http://paperclip.internal", raising=False)
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
        AsyncMock(side_effect=RuntimeError("paperclip down")),
    )

    with pytest.raises(agent_export.PaperclipUnavailableError, match="retry"):
        await agent_export.is_reserved_friday_agent(
            owner_id="user_abc",
            agent_id="my-custom-agent",
        )


@pytest.mark.asyncio
async def test_resolve_apex_ceo_degrades_to_none_when_paperclip_fetch_fails():
    """Unlike the Friday guard, org-chart placement is a nice-to-have default:
    a fetch failure yields None (unparented), never a blocked build."""

    async def _boom(*, owner_id):
        raise agent_export.PaperclipUnavailableError("down")

    assert await agent_export.resolve_apex_ceo_agent_id(owner_id="user_abc", fetch_paperclip_agents=_boom) is None


@pytest.mark.asyncio
async def test_is_reserved_friday_agent_false_when_no_ceo_in_list():
    fetch = _StubFetch(
        [
            {"id": "engineer-1", "role": "engineer"},
            {"id": "designer-1", "role": "designer"},
        ]
    )
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="engineer-1",
        fetch_paperclip_agents=fetch,
    )
    assert result is False


# ---------------------------------------------------------------------------
# Safety-scan integration: manifest.safety_flags
# ---------------------------------------------------------------------------


def test_export_attaches_empty_safety_flags_for_clean_workspace(fake_efs):
    """Clean workspace (no emails, no AWS keys) → manifest carries an
    empty safety_flags list. Field is always present so frontend can
    rely on it without nullity checks.
    """
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest.get("safety_flags") == []


def test_export_flags_email_in_persona_file(fake_efs, tmp_path):
    """An email address pasted into USER.md surfaces in manifest.safety_flags
    via the persona-format scan path (trust-seller posture — flag, don't
    auto-redact)."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "USER.md").write_text("## Owner\n- Email: alice@example.com\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "email-address" and "USER.md" in f["file"] for f in flags), flags


def test_export_flags_aws_key_in_memory_file(fake_efs, tmp_path):
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "MEMORY.md").write_text("## Notes\nOld key was AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "aws-secret-key" and f["severity"] == "high" for f in flags), flags


def test_export_scans_every_shipped_text_file_not_just_persona_and_md(fake_efs):
    """A bare provider token (or curl|bash) ships just as readily in a .sh/.py
    as in openclaw.json — the content scan used to skip everything but
    persona/openclaw.json/*.md, so a token in scripts/setup.sh sailed through
    the wizard unflagged."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "scripts" / "setup.sh").write_text(
        "#!/bin/sh\nexport TOKEN=xoxb-1234567890-abcdefghijklmnop\n", encoding="utf-8"
    )
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "provider-token" and "setup.sh" in f["file"] for f in flags), flags


def test_export_scan_skips_oversized_and_binary_files(fake_efs):
    """The generic bucket is size-capped and binary-sniffed: a >1MB text blob
    and a NUL-carrying binary must produce no content flags (media/data, not
    config)."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "big.log").write_text("sk-" + "A1b" * 12 + "\n" + "x" * (1024 * 1024 + 10), encoding="utf-8")
    (agent_dir / "blob.dat").write_bytes(b"\x00\x01sk-" + b"A1b" * 12)
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert not any(f["file"] in ("big.log", "blob.dat") for f in flags), flags


def test_export_safety_flag_schema(fake_efs, tmp_path):
    """SafetyFlag is JSON-serializable (dataclass-as-dict). Manifest
    consumers (frontend + admin preview) read pattern/severity/file/line/snippet."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "USER.md").write_text("alice@example.com\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert flags, "expected at least one flag"
    f = flags[0]
    assert {"pattern", "severity", "file", "line", "snippet"}.issubset(f.keys())


# ---------------------------------------------------------------------------
# enumerate_for_preview — dry-run preview backing the publish wizard
# ---------------------------------------------------------------------------


def test_enumerate_for_preview_lists_available_mcps(fake_efs, tmp_path):
    seller_id, agent_id, _ = fake_efs
    # Add MCPs to the seller's openclaw.json
    user_config = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(user_config.read_text())
    cfg["mcp"] = {"servers": {"jira-mcp": {"command": "node"}, "slack-mcp": {"command": "node"}}}
    user_config.write_text(json.dumps(cfg))

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    assert sorted(preview["available_mcps"]) == ["jira-mcp", "slack-mcp"]


def test_enumerate_for_preview_classifies_each_skill(fake_efs, tmp_path):
    """available_skills lists the container's global skills.entries (the
    selectable additions); workspace skills auto-ship separately."""
    seller_id, agent_id, agent_dir = fake_efs
    user_config = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(user_config.read_text())
    cfg["skills"] = {"entries": {"jira": {"apiKey": "secret"}}, "allowBundled": ["search"]}
    user_config.write_text(json.dumps(cfg))
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "custom-skill.md").write_text("# custom skill\n", encoding="utf-8")

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    by_name = {s["name"]: s["bucket"] for s in preview["available_skills"]}
    assert by_name == {"jira": "entries"}
    assert "custom-skill" in preview["auto_ship_skills"]
    assert "search" not in by_name


def test_enumerate_for_preview_lists_persona_files_present(fake_efs, tmp_path):
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "IDENTITY.md").write_text("I am.\n", encoding="utf-8")
    (agent_dir / "USER.md").write_text("Owner stuff.\n", encoding="utf-8")
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    persona = preview["persona_files"]
    assert "IDENTITY.md" in persona
    assert "USER.md" in persona
    # SOUL.md not present → not listed.
    assert "SOUL.md" not in persona


def test_enumerate_for_preview_carries_safety_flags(fake_efs, tmp_path):
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "USER.md").write_text("Email: alice@example.com\n", encoding="utf-8")
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    flags = preview["safety_flags"]
    assert any(f["pattern"] == "email-address" for f in flags)


# ---------------------------------------------------------------------------
# Slice credential-leak gate — export RAISES, preview SURFACES (never raises).
#
# The published openclaw-slice.json is a separate S3 artifact the tarball
# safety scan never walks. If a credential survives catalog_slice's scrub,
# export_agent_from_efs must abort BEFORE building the artifact; preview must
# surface the same finding inline without raising so the seller fixes it first.
# ---------------------------------------------------------------------------


def _plant_leaking_mcp(tmp_path: Path, seller_id: str, *, env_value: str = "sk_live_LEAK"):
    """Add an mcp server whose env carries a real (unscrubbed) secret to the
    seller's openclaw.json. We force the leak by patching _scrub_secret_fields
    to a no-op so we exercise the SAFETY gate, not the scrub itself.
    """
    cfg_path = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {"servers": {"leaky": {"command": "node", "env": {"API_KEY": env_value}}}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def test_export_raises_when_slice_credential_survives_scrub(fake_efs, tmp_path):
    """A credential surviving the scrub in the published slice blocks publish:
    export_agent_from_efs raises CredentialLeakError and no artifact is built.

    We neutralize the scrub (no-op) so a non-empty secret reaches the gate —
    this proves the gate catches what a missed/incomplete scrub would let
    through. The raised message names the field PATH but never the secret.
    """
    seller_id, agent_id, _ = fake_efs
    _plant_leaking_mcp(tmp_path, seller_id, env_value="sk_live_DO_NOT_LEAK_123")

    # Force a scrub miss: scrub becomes identity so the secret survives.
    with patch("core.services.catalog_slice._scrub_secret_fields", side_effect=lambda e: e):
        with pytest.raises(agent_export.CredentialLeakError) as exc_info:
            agent_export.export_agent_from_efs(
                seller_id=seller_id,
                agent_id=agent_id,
                selected_mcps=["leaky"],
            )

    msg = str(exc_info.value)
    # Path is named so the seller knows where to look...
    assert "mcp.servers.leaky.env" in msg, msg
    # ...but the raw secret never appears in the error.
    assert "sk_live_DO_NOT_LEAK_123" not in msg, msg


def test_export_raises_when_arg_credential_present(fake_efs, tmp_path):
    """A bare credential-shaped token in an MCP server's ``args`` (no secret
    flag, so the real arg scrub leaves it) is caught by the gate end-to-end:
    export_agent_from_efs raises CredentialLeakError naming the args path,
    never the secret. Real scrub runs (not patched) — this proves scrub+gate
    compose for the args path.
    """
    seller_id, agent_id, _ = fake_efs
    cfg_path = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {"servers": {"leaky": {"command": "npx", "args": ["-y", "sk-ant-DO_NOT_LEAK_0123456789ABCDEFabcdef"]}}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    with pytest.raises(agent_export.CredentialLeakError) as exc_info:
        agent_export.export_agent_from_efs(
            seller_id=seller_id,
            agent_id=agent_id,
            selected_mcps=["leaky"],
        )

    msg = str(exc_info.value)
    assert "mcp.servers.leaky.args" in msg, msg
    assert "DO_NOT_LEAK" not in msg, msg


def test_export_clean_slice_does_not_raise(fake_efs, tmp_path):
    """A properly-scrubbed slice (the normal path) builds the artifact with no
    CredentialLeakError. The real scrub blanks env values to "".
    """
    seller_id, agent_id, _ = fake_efs
    _plant_leaking_mcp(tmp_path, seller_id, env_value="sk_live_will_be_scrubbed")

    # No scrub patch → the real _scrub_secret_fields runs and empties the env.
    pkg = agent_export.export_agent_from_efs(
        seller_id=seller_id,
        agent_id=agent_id,
        selected_mcps=["leaky"],
    )
    # Slice shipped, env blanked.
    assert pkg.openclaw_slice["mcp"]["servers"]["leaky"]["env"] == {"API_KEY": ""}


def test_preview_surfaces_slice_leak_without_raising(fake_efs, tmp_path):
    """Preview must NOT raise on a leaking slice — it surfaces the finding
    inline so the seller fixes it before submit (where export raises).
    """
    seller_id, agent_id, _ = fake_efs
    _plant_leaking_mcp(tmp_path, seller_id, env_value="sk_live_PREVIEW_LEAK")

    with patch("core.services.catalog_slice._scrub_secret_fields", side_effect=lambda e: e):
        # Must NOT raise.
        preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)

    flags = preview["safety_flags"]
    assert any(f["pattern"] == "unscrubbed-credential" for f in flags), flags
    # Redaction holds in the preview surface too.
    for f in flags:
        if f["pattern"] == "unscrubbed-credential":
            assert "sk_live_PREVIEW_LEAK" not in f["snippet"], f


def test_preview_workspace_skills_autoship_global_entries_addable(monkeypatch, tmp_path):
    # Dir name == the config agent id, mirroring production (the workspace dir
    # name IS the agent's id in agents.list).
    agent_dir = tmp_path / "agent_x"
    (agent_dir / "skills" / "researcher").mkdir(parents=True)
    (agent_dir / "skills" / "writer").mkdir(parents=True)
    monkeypatch.setattr(agent_export, "_resolve_agent_dir", lambda *, seller_id, agent_id, config=None: agent_dir)

    config = {
        "agents": {"list": [{"id": "agent_x"}]},
        "skills": {"entries": {"image-lab": {"enabled": True}}},  # global, addable
    }

    class _WS:
        def read_openclaw_config(self, _):
            return config

    monkeypatch.setattr(agent_export, "get_workspace", lambda: _WS())

    out = agent_export.enumerate_for_preview(seller_id="u1", agent_id="agent_x")
    assert sorted(out["auto_ship_skills"]) == ["researcher", "writer"]
    assert [s["name"] for s in out["available_skills"]] == ["image-lab"]
    assert all(s["name"] not in ("researcher", "writer") for s in out["available_skills"])


# ---------------------------------------------------------------------------
# capture_agent_routines — marketplace publish routine capture
# ---------------------------------------------------------------------------


class _StubRoutineFetch:
    """Async-callable stub for the routine-detail fetcher."""

    def __init__(self, details):
        self.details = details
        self.calls = 0

    async def __call__(self, *, owner_id, agent_id):
        self.calls += 1
        return self.details


@pytest.mark.asyncio
async def test_capture_routines_strips_to_portable_shape():
    """Each schedule trigger becomes a {title, description, cron, timezone}
    entry; company/agent/instance ids and revision fields are dropped."""
    fetch = _StubRoutineFetch(
        [
            {
                "id": "routine-uuid",
                "companyId": "co-uuid",
                "assigneeAgentId": "agent-uuid",
                "latestRevisionId": "rev-uuid",
                "title": "Morning sweep",
                "description": "do it",
                "triggers": [
                    {
                        "id": "trig-uuid",
                        "kind": "schedule",
                        "cronExpression": "0 6 * * *",
                        "timezone": "UTC",
                        "enabled": True,
                    }
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == [
        {
            "title": "Morning sweep",
            "description": "do it",
            "cron": "0 6 * * *",
            "timezone": "UTC",
        }
    ]
    # No ids / revision fields leak through.
    assert all(k in {"title", "description", "cron", "timezone"} for entry in out for k in entry)


@pytest.mark.asyncio
async def test_capture_routines_skips_non_schedule_and_wrong_agent():
    fetch = _StubRoutineFetch(
        [
            # Wrong agent — skipped.
            {
                "assigneeAgentId": "other-agent",
                "title": "X",
                "triggers": [{"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"}],
            },
            # Right agent but webhook trigger — no cron to port, skipped.
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Webhook one",
                "triggers": [{"kind": "webhook"}],
            },
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == []


@pytest.mark.asyncio
async def test_capture_routines_returns_empty_on_fetch_failure():
    """A Paperclip-side failure must never break the export — returns []."""

    async def _boom(*, owner_id, agent_id):
        raise RuntimeError("paperclip down")

    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=_boom,
    )
    assert out == []


@pytest.mark.asyncio
async def test_capture_routines_multiple_schedule_triggers_flatten():
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Two-trigger",
                "description": "d",
                "triggers": [
                    {"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"},
                    {"kind": "schedule", "cronExpression": "0 8 * * 1", "timezone": "UTC"},
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert [e["cron"] for e in out] == ["0 6 * * *", "0 8 * * 1"]
    assert all(e["title"] == "Two-trigger" for e in out)


@pytest.mark.asyncio
async def test_capture_routines_carries_webhook_triggers_as_requirements():
    """A webhook automation ships as a REQUIREMENT, never as a live trigger:
    the (provider, event) pair is recovered by inverting the seed's title
    rendering, and the seller's resource (repo/mailbox/page) never ports."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "GitHub: Issue opened or changed",
                "description": "triage it",
                "triggers": [{"id": "t1", "kind": "webhook", "signingMode": "github_hmac"}],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == [
        {
            "title": "GitHub: Issue opened or changed",
            "kind": "webhook",
            "provider": "github",
            "event_id": "issues",
        }
    ]


@pytest.mark.asyncio
async def test_capture_routines_never_publishes_a_seeded_automations_description():
    """The seed patches the seller's own Google account, proxy URL and watch
    state-file path INTO the routine description — and manifest.json is served
    publicly by ``GET /marketplace/listings/{id}``. Routine capture runs after
    the tarball safety scan, so nothing else would catch it: the requirement
    carries the registry-rendered title and nothing of the seller's."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Google: Calendar changed",
                "description": (
                    "Every run, FIRST do maintenance: read "
                    "/home/node/.openclaw/google-watch/pub_seller123.json ... "
                    "--address https://hooks.isol8.co/t/pub_seller123 -a seller@acme.com"
                ),
                "triggers": [
                    {"kind": "webhook"},
                    # The self-renewal the google shapes attach as a SECOND
                    # trigger on the SAME routine. Exporting it as an ordinary
                    # schedule would recreate a dormant routine on the buyer
                    # that re-arms a watch they do not have — and carry the
                    # same description with it.
                    {"kind": "schedule", "cronExpression": "0 6 * * 1,4", "timezone": "UTC"},
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == [
        {
            "title": "Google: Calendar changed",
            "kind": "webhook",
            "provider": "google",
            "event_id": "calendar.changed",
        }
    ]
    assert "seller@acme.com" not in json.dumps(out)
    assert "pub_seller123" not in json.dumps(out)


@pytest.mark.asyncio
async def test_a_renamed_watch_routine_still_ports_nothing_of_the_seller():
    """A title is MUTABLE — the routine detail panel supports title-only
    edits. Rename a seeded Google watch and a title-keyed rule would hand it
    to the ordinary schedule branch, publishing the renewal cron plus a
    description holding the seller's mailbox and proxy URL. The webhook
    trigger is what a rename cannot touch, so that is what decides."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "My calendar thing",
                "description": "read /home/node/.openclaw/google-watch/pub_seller123.json ... -a seller@acme.com",
                "triggers": [
                    {"kind": "webhook"},
                    {"kind": "schedule", "cronExpression": "0 6 * * 1,4", "timezone": "UTC"},
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    # Unnameable as a requirement (the title no longer inverts) and unsafe as
    # a schedule — so it ports as nothing at all.
    assert out == []


@pytest.mark.asyncio
async def test_a_description_carrying_the_sellers_email_is_not_published():
    """Routine capture runs AFTER scan_tarball, so a description is the one
    seller-authored string reaching the public manifest unscanned. It gets the
    same PII patterns the tarball does — which also catches the schedule-only
    poll shape after a rename, where no trigger gives the automation away."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Check the mail",
                "description": "poll seller@acme.com every ten minutes and triage",
                "triggers": [{"kind": "schedule", "cronExpression": "*/10 * * * *", "timezone": "UTC"}],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    # The routine still ports — it is a real schedule — but stripped of the
    # text that names a person.
    assert out == [{"title": "Check the mail", "description": "", "cron": "*/10 * * * *", "timezone": "UTC"}]


@pytest.mark.asyncio
async def test_a_dormant_recreate_keeps_the_sellers_timezone():
    """``build_dormant_trigger_body`` defaults to UTC, so a spec that drops the
    captured timezone recreates a seller's 9am-local routine as 9am UTC — it
    fires at the wrong hour the moment the buyer enables it."""
    from core.services.agent_routine_provisioning import create_agent_routines

    admin = MagicMock()
    admin.list_routines = AsyncMock(return_value=[])
    admin.create_routine = AsyncMock(return_value={"id": "rt_1"})
    admin.create_routine_trigger = AsyncMock(return_value={"id": "tr_1"})

    await create_agent_routines(
        admin=admin,
        session_cookie="ck",
        company_id="co_1",
        agent_id="agent_new",
        routines=[
            {"title": "Morning sweep", "description": "look", "cron": "0 9 * * *", "timezone": "Europe/Berlin"},
            {"title": "No tz", "description": "look", "cron": "0 9 * * *"},
        ],
    )
    bodies = [c.kwargs["body"] for c in admin.create_routine_trigger.await_args_list]
    assert [b["timezone"] for b in bodies] == ["Europe/Berlin", "UTC"]
    # Still dormant — carrying the timezone must not arm anything.
    assert all(b["enabled"] is False for b in bodies)


@pytest.mark.asyncio
async def test_an_ordinary_description_ports_intact():
    """The scan must not eat the prompt that makes a ported routine useful."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Morning sweep",
                "description": "Summarise what changed overnight and post it to the team.",
                "triggers": [{"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"}],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out[0]["description"] == "Summarise what changed overnight and post it to the team."


@pytest.mark.asyncio
async def test_capture_routines_ports_a_poll_automation_as_a_requirement_too():
    """The poll shape has no webhook trigger at all — just the */10 cron — but
    its description still names the seller's mailbox, and recreating the cron
    on a buyer polls an account they never connected. Recognition is by TITLE,
    so it lands as a requirement like every other seeded automation."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Google: New email (checked on a schedule)",
                "description": "watch seller@acme.com for new mail",
                "triggers": [{"kind": "schedule", "cronExpression": "*/10 * * * *", "timezone": "UTC"}],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == [
        {
            "title": "Google: New email (checked on a schedule)",
            "kind": "webhook",
            "provider": "google",
            "event_id": "gmail.poll",
        }
    ]
    assert "seller@acme.com" not in json.dumps(out)


@pytest.mark.asyncio
async def test_capture_routines_drops_webhook_titles_the_registry_cannot_resolve():
    """A hand-made webhook routine names no registry event — we drop it
    rather than guess at a provider the buyer would then be told to wire."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "My bespoke hook",
                "triggers": [{"kind": "webhook"}],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == []


@pytest.mark.asyncio
async def test_capture_routines_skips_archived():
    """``delete_routine`` archives rather than deletes, so an archived row is
    a routine the seller DELETED — republishing must not resurrect it into
    every buyer's manifest."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "status": "archived",
                "title": "Old sweep",
                "triggers": [{"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"}],
            },
            {
                "assigneeAgentId": "agent-uuid",
                "status": "active",
                "title": "Live sweep",
                "triggers": [{"kind": "schedule", "cronExpression": "0 7 * * *", "timezone": "UTC"}],
            },
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert [e["title"] for e in out] == ["Live sweep"]


# --------------------------------------------------------------------------- #
# Legacy slug → workspace-UUID resolution (republish orphan fix)
# --------------------------------------------------------------------------- #


def _mock_workspace(live_dirs):
    ws = MagicMock()
    ws.list_workspace_agent_dirs.return_value = list(live_dirs)
    return ws


def test_slugify():
    assert agent_export._slugify("Vera") == "vera"
    assert agent_export._slugify("Pitch + Scout") == "pitch-scout"
    assert agent_export._slugify("  My Agent!  ") == "my-agent"
    assert agent_export._slugify("") == ""


def test_resolve_workspace_id_direct_hit():
    """A live UUID resolves to itself — zero behavior change for current listings."""
    uuid = "f9fc524c-acef-4467-8387-91ac7cbd00ef"
    ws = _mock_workspace([uuid])
    cfg = {"agents": {"list": [{"id": uuid, "name": "Vera"}]}}
    with patch.object(agent_export, "get_workspace", return_value=ws):
        assert agent_export._resolve_workspace_id("seller", uuid, cfg) == uuid


def test_resolve_workspace_id_slug_fallback():
    """A legacy slug resolves to the live workspace UUID via the display name."""
    uuid = "f9fc524c-acef-4467-8387-91ac7cbd00ef"
    ws = _mock_workspace([uuid])
    cfg = {"agents": {"list": [{"id": uuid, "name": "Vera"}]}}
    with patch.object(agent_export, "get_workspace", return_value=ws):
        assert agent_export._resolve_workspace_id("seller", "vera", cfg) == uuid


def test_resolve_workspace_id_miss_returns_unchanged():
    """An unresolvable id is handed back so the downstream existence check
    raises the familiar 'agent dir not found' — no behavior change."""
    ws = _mock_workspace(["f9fc524c-acef-4467-8387-91ac7cbd00ef"])
    cfg = {"agents": {"list": [{"id": "f9fc524c-acef-4467-8387-91ac7cbd00ef", "name": "Vera"}]}}
    with patch.object(agent_export, "get_workspace", return_value=ws):
        assert agent_export._resolve_workspace_id("seller", "ghost", cfg) == "ghost"
        # No config to scan → also a miss (returns unchanged).
        assert agent_export._resolve_workspace_id("seller", "vera", None) == "vera"


def test_resolve_workspace_id_ambiguous_raises():
    """Two live agents slugifying the same way → refuse to guess."""
    a = "11111111-1111-1111-1111-111111111111"
    b = "22222222-2222-2222-2222-222222222222"
    ws = _mock_workspace([a, b])
    cfg = {"agents": {"list": [{"id": a, "name": "Vera"}, {"id": b, "name": "Vera"}]}}
    with patch.object(agent_export, "get_workspace", return_value=ws):
        with pytest.raises(agent_export.AgentNotFoundError, match="ambiguous"):
            agent_export._resolve_workspace_id("seller", "vera", cfg)


def test_resolve_workspace_id_ignores_non_live_config_ids():
    """A config entry whose id is NOT a live workspace dir (e.g. a traversal
    payload) can never be returned — the resolver only yields live-dir members."""
    ws = _mock_workspace(["uuid-a-1111-1111-1111-111111111111"])
    cfg = {"agents": {"list": [{"id": "../escape", "name": "Vera"}]}}
    with patch.object(agent_export, "get_workspace", return_value=ws):
        # '../escape' isn't a live dir → not a candidate → slug returned unchanged.
        assert agent_export._resolve_workspace_id("seller", "vera", cfg) == "vera"


def test_export_resolves_legacy_slug_to_uuid(fake_efs):
    """End-to-end: publishing by the display-name slug resolves to the live
    workspace dir and heals the manifest forward to that id."""
    seller_id, agent_id, _ = fake_efs  # dir/id = 'my-agent-001', name 'MyAgent'
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="myagent")
    assert pkg.manifest["agent_id"] == agent_id


def test_export_uuid_direct_unchanged(fake_efs):
    """Publishing by the live id is unchanged — manifest carries that id."""
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest["agent_id"] == agent_id


def test_export_unresolvable_slug_raises_not_found(fake_efs):
    """A slug with no matching live agent still 404s (the residual manual class)."""
    seller_id, _, _ = fake_efs
    with pytest.raises(agent_export.AgentNotFoundError):
        agent_export.export_agent_from_efs(seller_id=seller_id, agent_id="ghost")


async def test_friday_guard_closed_for_legacy_slug(tmp_path):
    """A legacy slug 'friday' resolves to the CEO's workspace UUID, so resolving
    BEFORE the Friday guard (as the router now does) catches it — closing the
    bypass where the raw slug slips past the guard's UUID comparison."""
    from core.containers import workspace as workspace_module

    friday_uuid = "f1111111-2222-3333-4444-555555555555"
    seller_id = "user_owner_x"
    user_root = tmp_path / "users" / seller_id
    (user_root / f"workspace-{friday_uuid}").mkdir(parents=True)
    (user_root / "openclaw.json").write_text(
        json.dumps({"agents": {"list": [{"id": friday_uuid, "name": "Friday"}]}}),
        encoding="utf-8",
    )
    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))

    async def _fetch(*, owner_id):
        return [{"id": friday_uuid, "role": "ceo"}]

    with patch.object(agent_export, "get_workspace", return_value=fake_ws):
        # The raw slug bypasses the guard (the CEO's id is a UUID, not 'friday').
        assert (
            await agent_export.is_reserved_friday_agent(
                owner_id=seller_id, agent_id="friday", fetch_paperclip_agents=_fetch
            )
            is False
        )
        # Resolving first maps 'friday' → the CEO UUID …
        resolved = agent_export.resolve_workspace_agent_id(seller_id, "friday")
        assert resolved == friday_uuid
        # … which the guard then catches.
        assert (
            await agent_export.is_reserved_friday_agent(
                owner_id=seller_id, agent_id=resolved, fetch_paperclip_agents=_fetch
            )
            is True
        )


def test_export_strips_package_json_declaring_openclaw_extensions(fake_efs):
    """A package.json carrying an ``openclaw.extensions`` declaration (nested
    or dotted-key, or unparseable — fail closed) registers a workspace plugin
    in the buyer's container, so it must not ship. A plain package.json is a
    legitimate workspace file and survives."""
    seller_id, agent_id, agent_dir = fake_efs
    (agent_dir / "package.json").write_text(
        '{"name": "x", "openclaw": {"extensions": {"main": "index.js"}}}',
        encoding="utf-8",
    )
    clean = agent_dir / "clean"
    clean.mkdir()
    (clean / "package.json").write_text('{"name": "y", "version": "1.0.0"}', encoding="utf-8")
    bad_nested = agent_dir / "vendor"
    bad_nested.mkdir()
    (bad_nested / "package.json").write_text("not json at all", encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    pkg_members = [n for n in names if n.rsplit("/", 1)[-1] == "package.json"]
    assert pkg_members == ["./clean/package.json"], names
