"""Tests for agent_export — Path B (snapshot an agent into a marketplace artifact).

Every workspace runs on AgentCore, so ``_resolve_agent_location`` always answers
``("s3", "<owner>/hermes/<agent_id>")`` and an export reads the seller's Hermes S3
profile — including the ``openclaw.json`` the slice is projected from, which used to be a
per-USER file on EFS and now lives in the profile like everything else.
"""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from core.config import settings  # noqa: E402
from core.services import agent_export  # noqa: E402


_HERMES_BUCKET = "test-replicas"


@pytest.fixture
def hermes_profile_s3(tmp_path: Path):
    """Serve an on-disk sample tree as the owner's Hermes S3 profile.

    Tests build their sample content on disk (far easier to read than a pile of
    ``put_object`` calls) at ``<tmp_path>/users/<owner>/workspace-<agent_id>/``;
    this fixture mirrors that tree into a moto bucket when the export asks for
    the profile, then runs the REAL ``_download_s3_profile``. So the profile's
    own exclusions -- top-level ``sessions/``/``logs/``/``mcp-tokens/``,
    dotfiles at any depth, ``request_dump_*`` -- apply exactly as in production.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_HERMES_BUCKET)
        real_download = agent_export._download_s3_profile

        def _serve_from_disk(prefix: str, dest: Path) -> None:
            owner_id, _hermes, agent_id = prefix.split("/", 2)
            src_dir = tmp_path / "users" / owner_id / f"workspace-{agent_id}"
            if src_dir.is_dir():
                # os.walk never follows symlinked dirs, and S3 has no symlinks:
                # a profile download only ever writes regular files.
                for root, _dirs, files in os.walk(src_dir):
                    for name in files:
                        path = Path(root) / name
                        if path.is_symlink():
                            continue
                        s3.put_object(
                            Bucket=_HERMES_BUCKET,
                            Key=f"{prefix}/{path.relative_to(src_dir).as_posix()}",
                            Body=path.read_bytes(),
                        )
            real_download(prefix, dest)

        with (
            patch.object(settings, "OWNER_STATE_BUCKET", _HERMES_BUCKET),
            patch.object(agent_export, "_download_s3_profile", _serve_from_disk),
        ):
            yield s3


@pytest.fixture
def fake_efs(tmp_path: Path):
    """Builds the seller's profile content on disk at
    ``<tmp_path>/users/<seller>/workspace-<agent-id>/``, which ``hermes_profile_s3``
    mirrors into a moto bucket and serves through the REAL ``_download_s3_profile``.

    ONE ``openclaw.json``, inside the profile. It used to be two files: a per-USER roster on
    EFS (the slice source) plus a per-agent one in the workspace dir (the manifest's display
    name). There is no per-user file any more -- ``_read_profile_openclaw_config`` reads the
    profile's own, so the roster and the identity live in the same object, which is what a
    migrated seller's profile actually carries.
    """
    seller_id = "user_seller_abc"
    agent_id = "my-agent-001"

    user_root = tmp_path / "users" / seller_id
    agent_dir = user_root / f"workspace-{agent_id}"
    agent_dir.mkdir(parents=True)

    (agent_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "identity": {"name": "MyAgent", "vibe": "helpful", "description": "An agent"},
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
    (agent_dir / "README.md").write_text("# Hello\n", encoding="utf-8")
    (agent_dir / "scripts").mkdir()
    (agent_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    # Junk dirs that should be skipped.
    (agent_dir / "__pycache__").mkdir()
    (agent_dir / "__pycache__" / "ignored.pyc").write_bytes(b"junk")
    (agent_dir / ".git").mkdir()
    (agent_dir / ".git" / "HEAD").write_bytes(b"junk")

    yield seller_id, agent_id, agent_dir


@pytest.fixture
def hermes_profile(fake_efs, hermes_profile_s3):
    """``fake_efs``'s sample tree, served to the export as the seller's Hermes
    S3 profile. Yields the same ``(seller_id, agent_id, agent_dir)`` triple."""
    yield fake_efs


def _list_tar_names(tar_bytes: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers()]


def test_export_happy_path(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    assert pkg.format == "openclaw"
    assert pkg.manifest["name"] == "MyAgent"
    assert pkg.manifest["agent_id"] == agent_id
    names = _list_tar_names(pkg.tarball_bytes)
    assert any("openclaw.json" in n for n in names)
    assert any("README.md" in n for n in names)
    assert any("scripts/run.sh" in n for n in names)


def test_export_manifest_carries_requirements(hermes_profile):
    seller_id, agent_id, agent_dir = hermes_profile
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
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
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


def test_preview_includes_requirements(hermes_profile):
    seller_id, agent_id, agent_dir = hermes_profile
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


def test_export_skips_junk_dirs(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any("__pycache__" in n for n in names)
    assert not any(".git" in n for n in names)


def test_export_prunes_runtime_scratch_dirs(hermes_profile):
    """Runtime scratch an agent leaves in the workspace root (run evidence,
    cached API responses) must not ship — even with a run-id suffix — while a
    legitimately-named seller dir survives. The durable convention is a
    dot-prefixed ``.run/`` (already excluded by the dotfile filter); this
    backstop covers the non-dot names agents invented before that convention.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    for scratch in ("run-evidence", "run-evidence-1a2b", "run-artifacts", "github-cache"):
        (agent_dir / scratch).mkdir()
        (agent_dir / scratch / "x.json").write_text("{}", encoding="utf-8")
    # The sanctioned dot-prefixed scratch dir is excluded too.
    (agent_dir / ".run").mkdir()
    (agent_dir / ".run" / "note.md").write_text("scratch", encoding="utf-8")
    # A legitimately-named seller subdir must still ship (arbitrary content).
    # Not named sessions/logs/mcp-tokens: those are the Hermes profile's own
    # runtime dirs, dropped by the download before the tarball filter runs.
    (agent_dir / "notes").mkdir()
    (agent_dir / "notes" / "kept.md").write_text("keep me", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    for scratch in ("run-evidence", "run-evidence-1a2b", "run-artifacts", "github-cache"):
        assert not any(scratch in n for n in names), f"{scratch!r} leaked: {names}"
    assert not any(n.startswith("./.run/") or "/.run/" in n for n in names), names
    assert any("notes/kept.md" in n for n in names), names


def test_export_skips_boot_and_workspace_plugin_files(hermes_profile):
    """``BOOT.md`` triggers an autonomous LLM run on every gateway restart
    (internal hooks are enabled fleet-wide) and ``openclaw.plugin.json`` makes
    a workspace dir loadable in-process code — both are seller-authored
    ingestion vectors into a buyer's container, so neither may ship, at any
    depth. ``BOOTSTRAP.md`` is a legitimate persona file and must survive.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "BOOT.md").write_text("boot instructions\n", encoding="utf-8")
    (agent_dir / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    (agent_dir / "BOOTSTRAP.md").write_text("first-run persona\n", encoding="utf-8")
    nested = agent_dir / "nested"
    nested.mkdir()
    (nested / "BOOT.md").write_text("nested boot\n", encoding="utf-8")
    (nested / "openclaw.plugin.json").write_text("{}", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any(n.rsplit("/", 1)[-1] == "BOOT.md" for n in names), names
    assert not any(n.rsplit("/", 1)[-1] == "openclaw.plugin.json" for n in names), names
    assert any(n.endswith("BOOTSTRAP.md") for n in names), names
    assert any("README.md" in n for n in names), names


def test_export_skips_uploads_dir(hermes_profile):
    """``uploads/`` is OpenClaw's per-workspace attachment/media staging dir —
    runtime user data, not part of the agent's portable definition. It must
    never ship in a marketplace artifact: doing so leaks the seller's users'
    uploaded attachments and drags along stale snapshots of files that live
    canonically in pipelines/scripts. The whole dir is stripped regardless of
    contents (unlike the ambiguous ``sessions/``/``agent/`` workspace names,
    which DO ship — see the regression test above).
    """
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "uploads").mkdir()
    (agent_dir / "uploads" / "user-photo.png").write_bytes(b"\x89PNG\r\n")
    (agent_dir / "uploads" / "stale-bundle.lobster").write_text(
        "curl https://api.perplexity.ai/chat/completions -d @-", encoding="utf-8"
    )
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any("uploads/" in n for n in names), names
    # Sanity: legitimate workspace content still ships.
    assert any("README.md" in n for n in names), names


def test_export_requires_desktop_defaults_false(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest["requires_desktop"] is False


def test_export_requires_desktop_when_publisher_declares(hermes_profile):
    """'Needs the desktop app' is a publisher-set flag (not scanned) — recorded
    on the manifest for buyer-facing disclosure."""
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id, requires_desktop=True)
    assert pkg.manifest["requires_desktop"] is True


def test_credential_filter_blocks_txt_pasted_secrets(hermes_profile):
    """Sellers commonly paste credentials into a .txt file
    (``secret.txt``, ``credentials.txt``, ``private-key.txt``). The
    content scan only inspects persona/.md/openclaw.json, so these slip
    through if .txt isn't a config-shape extension. Add .txt to the gate.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "secret.txt").write_text("aws_secret_key=hunter2\n", encoding="utf-8")
    (agent_dir / "credentials.txt").write_text("API_KEY=...", encoding="utf-8")
    (agent_dir / "private-key.txt").write_text("-----BEGIN-----", encoding="utf-8")
    # Non-credential .txt file still ships.
    (agent_dir / "notes.txt").write_text("# brainstorm", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    for forbidden in ("secret.txt", "credentials.txt", "private-key.txt"):
        assert not any(forbidden in n for n in names), f"{forbidden!r} leaked through the credential filter: {names}"
    assert any("notes.txt" in n for n in names), names


def test_credential_filter_blocks_extensionless_aws_credentials(hermes_profile):
    """The AWS-style ``credentials`` file (extensionless, e.g. copied from
    ``~/.aws/credentials``) must be blocked. A substring-based filter
    catches it; a tighter word-boundary filter with an early-return on
    extensionless files bypasses the token check.

    Also pins that an extensionless ``credentials-helper`` script does
    NOT get blocked — only the EXACT canonical credential filenames hit
    the extensionless branch.
    """
    seller_id, agent_id, agent_dir = hermes_profile
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

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    # Extensionless credential filenames are blocked.
    assert not any(n.endswith("/credentials") or n == "./credentials" for n in names), names
    assert not any(n.endswith("/secrets") or n == "./secrets" for n in names), names
    # Extensionless helper script still ships.
    assert any("credentials-helper" in n for n in names), names


def test_credential_filter_word_boundary_does_not_strip_skills(hermes_profile):
    """A naive substring-match on the basename drops a skill named
    ``secretary.md`` or ``secret-santa.md`` or a helper file
    ``credentials-helper.ts`` — yet ``_enumerate_workspace_skill_names``
    still classifies the skill as WORKSPACE bucket, leaving the buyer
    with a config slice referencing a file that wasn't actually shipped.

    Whole-token + extension-shape match keeps these legit files in.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "secretary.md").write_text("# Secretary skill", encoding="utf-8")
    (skills_dir / "secret-santa.md").write_text("# Secret Santa skill", encoding="utf-8")
    (skills_dir / "credentials-helper.ts").write_text("export const x = 1;", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)

    assert any("skills/secretary.md" in n for n in names), names
    assert any("skills/secret-santa.md" in n for n in names), names
    assert any("skills/credentials-helper.ts" in n for n in names), names


def test_export_skips_dotfiles_anywhere_in_tree(hermes_profile, tmp_path):
    """Regression: hidden files / hidden dirs at any depth must NOT ship in
    the marketplace artifact. Sellers commonly have .env, .ssh/*, .aws/
    credentials, .openclaw/secrets in their workspace; bundling them
    would deliver secrets directly to buyers.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    # Plant a bunch of dotfile / dotdir scenarios.
    (agent_dir / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    (agent_dir / ".ssh").mkdir()
    (agent_dir / ".ssh" / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    # Hidden file nested inside an otherwise-allowed directory.
    (agent_dir / "scripts" / ".secret").write_text("password=hunter2\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    # None of the dotfile paths show up.
    assert not any(".env" in n.split("/")[-1] for n in names if n != "."), names
    assert not any(".ssh" in n for n in names), names
    assert not any(".secret" in n for n in names), names


def test_export_skips_backup_and_temp_files(hermes_profile):
    """Regression: editing backups (*.bak, *.bak.<ts>, *.orig) and temp files
    (*.tmp, *~) ride along from in-place workspace edits but are inert cruft —
    they must never reach a buyer's installed workspace."""
    seller_id, agent_id, agent_dir = hermes_profile
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

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any(".bak" in n for n in names), names
    assert not any(n.endswith(".tmp") for n in names), names
    assert not any(n.endswith(".orig") for n in names), names
    assert not any(n.endswith("~") for n in names), names
    assert any(n.endswith("backup-notes.md") for n in names), names


def test_export_snapshot_is_deterministic(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    a = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    b = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    # Identical content → identical bytes (mtimes/uids zeroed).
    assert a.tarball_bytes == b.tarball_bytes


def test_export_skips_credential_shaped_filenames(hermes_profile):
    """Defense in depth on top of marketplace_safety's content scan: files
    whose names look like credentials (``resy-credentials.json``,
    ``aws.credentials.json``, ``*.secret.json``, ``id_rsa``, etc.) are
    skipped at tar time so the seller's account doesn't ship to every
    buyer if the scan ever misses the content pattern.
    """
    seller_id, agent_id, agent_dir = hermes_profile
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

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
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


def test_export_synthesizes_slice_from_seller_config(hermes_profile):
    """The CatalogPackage's openclaw_slice carries the seller's agent entry
    with user-specific fields stripped (model, channels, workspace, id) and
    the publisher's plugins + tools blocks intact. This is what makes the
    buyer-side deploy auto-register the new agent in their openclaw.json.
    """
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)

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


def test_export_replaces_seller_agent_id_in_mcp_args_with_sentinel(hermes_profile, tmp_path: Path):
    """Regression for marketplace-deploy-symmetry (spec 2026-05-18): the
    slice's mcp.servers.X.args paths reference the seller's workspace,
    e.g. ``/home/node/.openclaw/workspace-{seller_agent_id}/mcp-resy/index.js``.
    On deploy that path doesn't exist in the buyer's container. Replace
    the seller-keyed segment with __AGENT_DIR__ at export so deploy can
    substitute the buyer's new agent_id cleanly.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    # Seller's openclaw.json already has an mcp servers entry — make it
    # carry an args path that points at the seller's workspaces dir.
    cfg_path = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
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

    pkg = agent_export.export_agent_from_profile(
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


def test_export_normalizes_legacy_workspaces_layout_in_mcp_args(hermes_profile, tmp_path: Path):
    """Sellers whose containers predate the workspace-layout migration still
    carry the retired ``workspaces/{agent_id}`` spelling in MCP args. Export
    must normalize that OLD form to the same ``workspace-__AGENT_DIR__``
    sentinel the new form produces, so every artifact ships layout-current.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    cfg_path = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
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

    pkg = agent_export.export_agent_from_profile(
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


def test_the_slice_never_carries_cron_jobs(hermes_profile, tmp_path):
    """Agent scheduling is Paperclip routines, carried as ``manifest["routines"]``. The slice's
    ``cron_jobs`` is unconditionally empty -- an ``openclaw.json``-shaped cron block found
    anywhere in the seller's tree is NOT a source, so a stale one cannot ride into a buyer's
    workspace as a live schedule."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "cron").mkdir()
    (agent_dir / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "stale", "agentId": agent_id, "name": "morning standup"}]}),
        encoding="utf-8",
    )

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)

    assert pkg.openclaw_slice["cron_jobs"] == []


def test_a_hermes_native_profile_publishes(hermes_profile, tmp_path):
    """A Hermes-native profile has no openclaw.json. It used to be refused ("this Hermes profile
    has none"), so no seller could ever publish. The profile itself is the agent: its persona
    and skills ship, the slice is the identity entry, and the owner-keyed files the backend
    wrote (config.yaml, the facts file, CLI credentials, $HOME) never reach the artifact."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "openclaw.json").unlink()
    (agent_dir / "SOUL.md").write_text("You are a careful researcher.\n", encoding="utf-8")
    (agent_dir / "config.yaml").write_text("model: x\n", encoding="utf-8")
    (agent_dir / "lightbulb-facts.json").write_text("{}", encoding="utf-8")
    (agent_dir / "cli-env.json").write_text('{"GOG_KEYRING_PASSWORD": "pw"}', encoding="utf-8")
    for rel in ("skills/research/SKILL.md", "skills/instagram/token.sh", "home/notes.txt"):
        (agent_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent_dir / rel).write_text("x\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)

    names = {n.removeprefix("./") for n in _list_tar_names(pkg.tarball_bytes)}
    assert {"SOUL.md", "skills/research/SKILL.md", "README.md"} <= names
    for leaked in (
        "config.yaml",
        "lightbulb-facts.json",
        "cli-env.json",
        "skills/instagram/token.sh",
        "home/notes.txt",
    ):
        assert leaked not in names, leaked
    assert pkg.openclaw_slice["agent"] == {}  # the seller's id is stripped; the buyer mints its own
    assert pkg.openclaw_slice["cron_jobs"] == []
    assert pkg.manifest["name"] == agent_id

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    assert preview["auto_ship_skills"] == ["research"]


def test_export_agent_not_in_config_raises(hermes_profile, tmp_path):
    """The profile has an openclaw.json but its agents.list does not name this agent: fail
    loudly — we can't synthesize a slice without the agent's identity/skills/tools."""
    seller_id, _, _ = hermes_profile
    orphan_dir = tmp_path / "users" / seller_id / "workspace-orphan-agent-99"
    orphan_dir.mkdir()
    (orphan_dir / "README.md").write_text("orphan", encoding="utf-8")
    (orphan_dir / "openclaw.json").write_text(
        json.dumps({"agents": {"list": [{"id": "someone-else"}]}}), encoding="utf-8"
    )
    with pytest.raises(agent_export.AgentNotInConfigError, match="not registered"):
        agent_export.export_agent_from_profile(seller_id=seller_id, agent_id="orphan-agent-99")


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
async def test_reserved_default_agent_stays_blocked_after_rename():
    fetch = _StubFetch([{"id": "root-ceo-001", "role": "ceo", "name": "Nova"}])
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="root-ceo-001",
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


def test_export_attaches_empty_safety_flags_for_clean_workspace(hermes_profile):
    """Clean workspace (no emails, no AWS keys) → manifest carries an
    empty safety_flags list. Field is always present so frontend can
    rely on it without nullity checks.
    """
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest.get("safety_flags") == []


def test_export_flags_email_in_persona_file(hermes_profile, tmp_path):
    """An email address pasted into USER.md surfaces in manifest.safety_flags
    via the persona-format scan path (trust-seller posture — flag, don't
    auto-redact)."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "USER.md").write_text("## Owner\n- Email: alice@example.com\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "email-address" and "USER.md" in f["file"] for f in flags), flags


def test_export_flags_aws_key_in_memory_file(hermes_profile, tmp_path):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "MEMORY.md").write_text("## Notes\nOld key was AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "aws-secret-key" and f["severity"] == "high" for f in flags), flags


def test_export_scans_every_shipped_text_file_not_just_persona_and_md(hermes_profile):
    """A bare provider token (or curl|bash) ships just as readily in a .sh/.py
    as in openclaw.json — the content scan used to skip everything but
    persona/openclaw.json/*.md, so a token in scripts/setup.sh sailed through
    the wizard unflagged."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "scripts" / "setup.sh").write_text(
        "#!/bin/sh\nexport TOKEN=xoxb-1234567890-abcdefghijklmnop\n", encoding="utf-8"
    )
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert any(f["pattern"] == "provider-token" and "setup.sh" in f["file"] for f in flags), flags


def test_export_scan_skips_oversized_and_binary_files(hermes_profile):
    """The generic bucket is size-capped and binary-sniffed: a >1MB text blob
    and a NUL-carrying binary must produce no content flags (media/data, not
    config)."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "big.log").write_text("sk-" + "A1b" * 12 + "\n" + "x" * (1024 * 1024 + 10), encoding="utf-8")
    (agent_dir / "blob.dat").write_bytes(b"\x00\x01sk-" + b"A1b" * 12)
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert not any(f["file"] in ("big.log", "blob.dat") for f in flags), flags


def test_export_safety_flag_schema(hermes_profile, tmp_path):
    """SafetyFlag is JSON-serializable (dataclass-as-dict). Manifest consumers
    read pattern/severity/file/line — and NOT snippet, which never reaches a
    manifest (see the leak test below)."""
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "USER.md").write_text("alice@example.com\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert flags, "expected at least one flag"
    f = flags[0]
    assert {"pattern", "severity", "file", "line"}.issubset(f.keys())


def test_export_manifest_never_carries_snippets(hermes_profile):
    """The manifest is uploaded to S3 + persisted on the DDB listing row, and
    served verbatim by the UNAUTHENTICATED listing-detail route under a public
    cache header. ``snippet`` is 80 raw chars of the seller's source line — by
    construction the field most likely to hold a credential — so it must never
    ride into a manifest.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "MEMORY.md").write_text("key AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    (agent_dir / "USER.md").write_text("- Email: alice@example.com\n", encoding="utf-8")
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    flags = pkg.manifest.get("safety_flags", [])
    assert flags, "expected at least one flag"
    assert all("snippet" not in f for f in flags), flags
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(pkg.manifest)
    assert "alice@example.com" not in json.dumps(pkg.manifest)


# ---------------------------------------------------------------------------
# enumerate_for_preview — dry-run preview backing the publish wizard
# ---------------------------------------------------------------------------


def test_enumerate_for_preview_lists_available_mcps(hermes_profile, tmp_path):
    seller_id, agent_id, _ = hermes_profile
    # Add MCPs to the seller's openclaw.json
    user_config = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
    cfg = json.loads(user_config.read_text())
    cfg["mcp"] = {"servers": {"jira-mcp": {"command": "node"}, "slack-mcp": {"command": "node"}}}
    user_config.write_text(json.dumps(cfg))

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    assert sorted(preview["available_mcps"]) == ["jira-mcp", "slack-mcp"]


def test_enumerate_for_preview_classifies_each_skill(hermes_profile, tmp_path):
    """available_skills lists the container's global skills.entries (the
    selectable additions); workspace skills auto-ship separately."""
    seller_id, agent_id, agent_dir = hermes_profile
    user_config = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
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


def test_enumerate_for_preview_lists_persona_files_present(hermes_profile, tmp_path):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "IDENTITY.md").write_text("I am.\n", encoding="utf-8")
    (agent_dir / "USER.md").write_text("Owner stuff.\n", encoding="utf-8")
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    persona = preview["persona_files"]
    assert "IDENTITY.md" in persona
    assert "USER.md" in persona
    # SOUL.md not present → not listed.
    assert "SOUL.md" not in persona


def test_enumerate_for_preview_carries_safety_flags(hermes_profile, tmp_path):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "USER.md").write_text("Email: alice@example.com\n", encoding="utf-8")
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    flags = preview["safety_flags"]
    assert any(f["pattern"] == "email-address" for f in flags)


def test_enumerate_for_preview_keeps_snippets(hermes_profile):
    """The preview is authenticated + seller-scoped and shows the seller their
    OWN workspace lines so they can fix a finding before submit. It is never
    persisted, so — unlike the manifest — it keeps ``snippet``.
    """
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "USER.md").write_text("Email: alice@example.com\n", encoding="utf-8")
    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    flag = next(f for f in preview["safety_flags"] if f["pattern"] == "email-address")
    assert "alice@example.com" in flag["snippet"]


# ---------------------------------------------------------------------------
# Slice credential-leak gate — export RAISES, preview SURFACES (never raises).
#
# The published openclaw-slice.json is a separate S3 artifact the tarball
# safety scan never walks. If a credential survives catalog_slice's scrub,
# export_agent_from_profile must abort BEFORE building the artifact; preview must
# surface the same finding inline without raising so the seller fixes it first.
# ---------------------------------------------------------------------------


def _plant_leaking_mcp(tmp_path: Path, seller_id: str, agent_id: str, *, env_value: str = "sk_live_LEAK"):
    """Add an mcp server whose env carries a real (unscrubbed) secret to the profile's
    openclaw.json. We force the leak by patching _scrub_secret_fields to a no-op so we
    exercise the SAFETY gate, not the scrub itself.
    """
    cfg_path = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {"servers": {"leaky": {"command": "node", "env": {"API_KEY": env_value}}}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")


def test_export_raises_when_slice_credential_survives_scrub(hermes_profile, tmp_path):
    """A credential surviving the scrub in the published slice blocks publish:
    export_agent_from_profile raises CredentialLeakError and no artifact is built.

    We neutralize the scrub (no-op) so a non-empty secret reaches the gate —
    this proves the gate catches what a missed/incomplete scrub would let
    through. The raised message names the field PATH but never the secret.
    """
    seller_id, agent_id, _ = hermes_profile
    _plant_leaking_mcp(tmp_path, seller_id, agent_id, env_value="sk_live_DO_NOT_LEAK_123")

    # Force a scrub miss: scrub becomes identity so the secret survives.
    with patch("core.services.catalog_slice._scrub_secret_fields", side_effect=lambda e: e):
        with pytest.raises(agent_export.CredentialLeakError) as exc_info:
            agent_export.export_agent_from_profile(
                seller_id=seller_id,
                agent_id=agent_id,
                selected_mcps=["leaky"],
            )

    msg = str(exc_info.value)
    # Path is named so the seller knows where to look...
    assert "mcp.servers.leaky.env" in msg, msg
    # ...but the raw secret never appears in the error.
    assert "sk_live_DO_NOT_LEAK_123" not in msg, msg


def test_export_raises_when_arg_credential_present(hermes_profile, tmp_path):
    """A bare credential-shaped token in an MCP server's ``args`` (no secret
    flag, so the real arg scrub leaves it) is caught by the gate end-to-end:
    export_agent_from_profile raises CredentialLeakError naming the args path,
    never the secret. Real scrub runs (not patched) — this proves scrub+gate
    compose for the args path.
    """
    seller_id, agent_id, _ = hermes_profile
    cfg_path = tmp_path / "users" / seller_id / f"workspace-{agent_id}" / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {"servers": {"leaky": {"command": "npx", "args": ["-y", "sk-ant-DO_NOT_LEAK_0123456789ABCDEFabcdef"]}}}
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    with pytest.raises(agent_export.CredentialLeakError) as exc_info:
        agent_export.export_agent_from_profile(
            seller_id=seller_id,
            agent_id=agent_id,
            selected_mcps=["leaky"],
        )

    msg = str(exc_info.value)
    assert "mcp.servers.leaky.args" in msg, msg
    assert "DO_NOT_LEAK" not in msg, msg


def test_export_clean_slice_does_not_raise(hermes_profile, tmp_path):
    """A properly-scrubbed slice (the normal path) builds the artifact with no
    CredentialLeakError. The real scrub blanks env values to "".
    """
    seller_id, agent_id, _ = hermes_profile
    _plant_leaking_mcp(tmp_path, seller_id, agent_id, env_value="sk_live_will_be_scrubbed")

    # No scrub patch → the real _scrub_secret_fields runs and empties the env.
    pkg = agent_export.export_agent_from_profile(
        seller_id=seller_id,
        agent_id=agent_id,
        selected_mcps=["leaky"],
    )
    # Slice shipped, env blanked.
    assert pkg.openclaw_slice["mcp"]["servers"]["leaky"]["env"] == {"API_KEY": ""}


def test_preview_surfaces_slice_leak_without_raising(hermes_profile, tmp_path):
    """Preview must NOT raise on a leaking slice — it surfaces the finding
    inline so the seller fixes it before submit (where export raises).
    """
    seller_id, agent_id, _ = hermes_profile
    _plant_leaking_mcp(tmp_path, seller_id, agent_id, env_value="sk_live_PREVIEW_LEAK")

    with patch("core.services.catalog_slice._scrub_secret_fields", side_effect=lambda e: e):
        # Must NOT raise.
        preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)

    flags = preview["safety_flags"]
    assert any(f["pattern"] == "unscrubbed-credential" for f in flags), flags
    # Redaction holds in the preview surface too.
    for f in flags:
        if f["pattern"] == "unscrubbed-credential":
            assert "sk_live_PREVIEW_LEAK" not in f["snippet"], f


def test_preview_workspace_skills_autoship_global_entries_addable(monkeypatch, tmp_path, hermes_profile_s3):
    # Dir name == the config agent id, mirroring production (the workspace dir
    # name IS the agent's id in agents.list).
    agent_dir = tmp_path / "users" / "u1" / "workspace-agent_x"
    (agent_dir / "skills" / "researcher").mkdir(parents=True)
    (agent_dir / "skills" / "writer").mkdir(parents=True)
    # A skill dir needs a file: S3 has no empty directories, so an empty one
    # would not survive the profile round trip.
    (agent_dir / "skills" / "researcher" / "SKILL.md").write_text("# researcher", encoding="utf-8")
    (agent_dir / "skills" / "writer" / "SKILL.md").write_text("# writer", encoding="utf-8")

    # The profile's own openclaw.json is the config source now -- there is no per-user file.
    (agent_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"list": [{"id": "agent_x"}]},
                "skills": {"entries": {"image-lab": {"enabled": True}}},  # global, addable
            }
        ),
        encoding="utf-8",
    )

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
                "triggers": [
                    {
                        "id": "t1",
                        "kind": "webhook",
                        "signingMode": "github_hmac",
                        "label": "GitHub: Issue opened or changed",
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
                    {"kind": "webhook", "label": "Google: Calendar changed"},
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
async def test_a_renamed_watch_routine_is_still_recognised_by_its_trigger():
    """A routine title is MUTABLE — the detail panel supports title-only
    edits. Rename a seeded Google watch and a title-keyed rule would hand it
    to the ordinary schedule branch, publishing the renewal cron plus a
    description holding the seller's mailbox and proxy URL. The rename never
    reaches the TRIGGER, whose label the seed set from the registry — so the
    automation is still recognised, still named correctly, and still ports
    none of the seller."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "My calendar thing",
                "description": "read /home/node/.openclaw/google-watch/pub_seller123.json ... -a seller@acme.com",
                "triggers": [
                    {"kind": "webhook", "label": "Google: Calendar changed"},
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
    assert "My calendar thing" not in json.dumps(out)


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
    # The whole prompt was the thing naming a person, so once it is gone there
    # is no portable routine left — a bare cron with no instructions looks
    # like work and is not.
    assert out == []


@pytest.mark.asyncio
async def test_a_routine_titled_after_the_seller_does_not_publish_at_all():
    """The title reaches the public manifest exactly like the description
    does, and it cannot be blanked and still name anything — so an unsafe one
    takes the routine with it rather than publishing the seller."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "sweep for seller@acme.com",
                "description": "look at the board",
                "triggers": [{"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"}],
            },
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Morning sweep",
                "description": "look at the board",
                "triggers": [{"kind": "schedule", "cronExpression": "0 7 * * *", "timezone": "UTC"}],
            },
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert [e["title"] for e in out] == ["Morning sweep"]
    assert "seller@acme.com" not in json.dumps(out)


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
async def test_a_registry_title_alone_does_not_make_a_routine_an_automation():
    """A title is guessable as well as editable — nothing stops a seller
    titling an ordinary cron exactly like a registry automation. Classifying
    on the title would publish it as a setup task the buyer cannot complete
    and throw the real schedule away, so the webhook trigger is the only
    signal. Its description is still scrubbed, which is what keeps the
    schedule-only poll shape safe without a title rule."""
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
    # Not a requirement (no webhook trigger to recognise) and not a schedule
    # either: its entire prompt was the seller's mailbox, so what would be
    # left is a titled cron with no instructions — something that looks like
    # work and is not.
    assert out == []


@pytest.mark.asyncio
async def test_a_credential_in_a_description_is_not_published_either():
    """The persona pattern set is not only PII — it carries
    ``hardcoded-secret``/``aws-secret-key``/``provider-token`` too, and
    routine capture is appended after ``safety_flags`` are generated, so a
    credential here would not even reach moderation. Any flag drops the text —
    and with the instructions gone there is no portable routine left."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Nightly push",
                "description": 'upload the report using api_key = "sk-live-abcdefghijklmnopqrstuvwxyz0123"',
                "triggers": [{"kind": "schedule", "cronExpression": "0 6 * * *", "timezone": "UTC"}],
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
async def test_a_hand_built_webhook_routine_keeps_its_real_schedule():
    """The "Any other app" rung creates webhook triggers with NO label, and
    the detail panel lets the owner add a schedule to the same routine. That
    schedule is legitimate work — treating any webhook trigger as seeded
    provenance would silently drop it from every hire."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "Ping me when the CRM fires",
                "description": "Post a summary to the team channel.",
                "triggers": [
                    {"kind": "webhook", "signingMode": "hmac_sha256"},
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
    assert out == [
        {
            "title": "Ping me when the CRM fires",
            "description": "Post a summary to the team channel.",
            "cron": "0 8 * * 1",
            "timezone": "UTC",
        }
    ]


@pytest.mark.asyncio
async def test_a_requirement_is_named_by_the_registry_not_by_the_seller():
    """The seed puts the registry-rendered title on the trigger's ``label``,
    which a routine rename never reaches. Naming the requirement from the
    label rather than the routine's title means no seller-authored string
    reaches the public manifest at all — and the rename still resolves."""
    fetch = _StubRoutineFetch(
        [
            {
                "assigneeAgentId": "agent-uuid",
                "title": "seller@acme.com's issue triage",
                "description": "triage it",
                "triggers": [
                    {"kind": "webhook", "label": "GitHub: Issue opened or changed"},
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
            "title": "GitHub: Issue opened or changed",
            "kind": "webhook",
            "provider": "github",
            "event_id": "issues",
        }
    ]
    assert "seller@acme.com" not in json.dumps(out)


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
# Agent-id resolution. The legacy slug -> workspace-UUID layer is GONE: a Hermes profile
# prefix IS the canonical agent id, so there are no slugs to forward and
# ``resolve_workspace_agent_id`` only validates. The tests that pinned ``_resolve_workspace_id``
# and ``_slugify`` went with them.
# --------------------------------------------------------------------------- #


def test_export_uuid_direct_unchanged(hermes_profile):
    """Publishing by the live id is unchanged — manifest carries that id."""
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    assert pkg.manifest["agent_id"] == agent_id


async def test_friday_guard_catches_the_ceo_by_its_canonical_id():
    """The guard compares the agent id to the CEO's Paperclip id, and that id is the ONLY way
    to name an agent now: a profile prefix is the canonical id, so the legacy-slug bypass the
    resolver used to close is unreachable -- a slug names no profile and the download raises
    ``AgentNotFoundError`` long before the guard would be asked."""
    friday_uuid = "f1111111-2222-3333-4444-555555555555"

    async def _fetch(*, owner_id):
        return [{"id": friday_uuid, "role": "ceo"}]

    assert (
        await agent_export.is_reserved_friday_agent(
            owner_id="user_owner_x", agent_id=friday_uuid, fetch_paperclip_agents=_fetch
        )
        is True
    )
    assert (
        await agent_export.is_reserved_friday_agent(
            owner_id="user_owner_x", agent_id="some-other-agent", fetch_paperclip_agents=_fetch
        )
        is False
    )


def test_export_strips_package_json_declaring_openclaw_extensions(hermes_profile):
    """A package.json carrying an ``openclaw.extensions`` declaration (nested
    or dotted-key, or unparseable — fail closed) registers a workspace plugin
    in the buyer's container, so it must not ship. A plain package.json is a
    legitimate workspace file and survives."""
    seller_id, agent_id, agent_dir = hermes_profile
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

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    pkg_members = [n for n in names if n.rsplit("/", 1)[-1] == "package.json"]
    assert pkg_members == ["./clean/package.json"], names


@pytest.mark.asyncio
async def test_capture_routines_ports_variable_definitions_with_defaults():
    """Variable definitions (with defaults) ride each schedule entry — without
    them, Paperclip re-syncs {{NAME}} references as required defaultless
    variables and the buyer-side dormant schedule trigger 422s."""
    fetch = _StubRoutineFetch(
        [
            {
                "id": "routine-uuid",
                "assigneeAgentId": "agent-uuid",
                "title": "Finance snapshot",
                "description": "Cap is {{MONTHLY_SPEND_CAP_USD}}.",
                "variables": [
                    {
                        "name": "MONTHLY_SPEND_CAP_USD",
                        "type": "number",
                        "defaultValue": 2000,
                        "required": True,
                        "instanceOnlyNoise": "drop-me",
                    }
                ],
                "triggers": [
                    {"kind": "schedule", "cronExpression": "0 7 * * 1", "timezone": "America/Aruba"},
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert len(out) == 1
    assert out[0]["variables"] == [
        {"name": "MONTHLY_SPEND_CAP_USD", "type": "number", "defaultValue": 2000, "required": True}
    ]


@pytest.mark.asyncio
async def test_capture_routines_drops_routine_when_variable_default_is_unsafe():
    """A variable default that fails the safety scan takes the routine with it
    — porting without the default recreates the 422, porting without the
    variable ships a contract referencing an input the buyer lacks."""
    fetch = _StubRoutineFetch(
        [
            {
                "id": "routine-uuid",
                "assigneeAgentId": "agent-uuid",
                "title": "Leaky",
                "description": "Uses {{TOKEN}}.",
                "variables": [
                    {
                        "name": "TOKEN",
                        "type": "text",
                        "defaultValue": "AKIAABCDEFGHIJKLMNOP secret",
                        "required": True,
                    }
                ],
                "triggers": [
                    {"kind": "schedule", "cronExpression": "0 7 * * 1", "timezone": "UTC"},
                ],
            }
        ]
    )
    out = await agent_export.capture_agent_routines(
        owner_id="user_abc",
        agent_id="agent-uuid",
        fetch_routine_details=fetch,
    )
    assert out == []


# ---------------------------------------------------------------------------
# Explicit include list (``paths``), nested repo-checkout pruning, export cap
# ---------------------------------------------------------------------------


def _add_repo_checkout(agent_dir: Path, rel: str, *, gitfile: bool = False) -> None:
    """A cloned repo inside the workspace: ``.git`` (dir, or a worktree-style
    gitfile) plus a working tree that must never reach a buyer."""
    repo = agent_dir / rel
    (repo / "src").mkdir(parents=True)
    if gitfile:
        (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    else:
        (repo / ".git").mkdir()
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "src" / "main.py").write_text("print('private')\n", encoding="utf-8")
    (repo / "README.md").write_text("# private repo\n", encoding="utf-8")


def test_paths_ships_only_listed_entries_and_recurses_dirs(hermes_profile):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "reports").mkdir()
    (agent_dir / "reports" / "finance.md").write_text("revenue\n", encoding="utf-8")

    pkg = agent_export.export_agent_from_profile(
        seller_id=seller_id, agent_id=agent_id, paths=["README.md", "scripts/"]
    )

    names = _list_tar_names(pkg.tarball_bytes)
    assert "./README.md" in names
    assert "./scripts/run.sh" in names
    assert "./openclaw.json" not in names
    assert "./reports/finance.md" not in names
    assert pkg.tarball_contents == ["./README.md", "./scripts/run.sh"]
    assert pkg.manifest["file_count"] == 2
    assert pkg.pruned_paths == []


def test_paths_ancestor_and_descendant_do_not_double_ship(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    pkg = agent_export.export_agent_from_profile(
        seller_id=seller_id, agent_id=agent_id, paths=["scripts", "scripts/run.sh"]
    )
    assert _list_tar_names(pkg.tarball_bytes).count("./scripts/run.sh") == 1


def test_paths_rejects_traversal_absolute_and_missing(hermes_profile):
    seller_id, agent_id, _ = hermes_profile
    with pytest.raises(agent_export.InvalidExportPathsError) as exc:
        agent_export.export_agent_from_profile(
            seller_id=seller_id,
            agent_id=agent_id,
            paths=["README.md", "../openclaw.json", "/etc/passwd", "never-there.md", "scripts/../../x"],
        )
    msg = str(exc.value)
    for bad in ("../openclaw.json", "/etc/passwd", "never-there.md", "scripts/../../x"):
        assert bad in msg
    assert "'README.md'" not in msg


def test_explicit_paths_ship_a_checkouts_working_tree_but_never_its_git_dir(hermes_profile):
    """Naming a checkout in ``paths`` is the seller opting in: the working
    tree ships, ``.git`` itself still falls to the junk-dir skip."""
    seller_id, agent_id, agent_dir = hermes_profile
    _add_repo_checkout(agent_dir, "isol8")

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id, paths=["isol8"])

    names = _list_tar_names(pkg.tarball_bytes)
    assert "./isol8/src/main.py" in names
    assert "./isol8/README.md" in names
    assert not any(".git" in n for n in names)
    assert pkg.pruned_paths == []


def test_file_cap_stops_the_walk_immediately(hermes_profile, monkeypatch):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "out").mkdir()
    for i in range(50):
        (agent_dir / "out" / f"run-{i:03d}.log").write_text("x" * 500, encoding="utf-8")
    monkeypatch.setattr(agent_export, "_MAX_EXPORT_FILES", 5)

    with pytest.raises(agent_export.ExportTooLargeError) as exc:
        agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)

    # Raised on the first file past the cap, not after tarring all 50+.
    assert exc.value.file_count == 6
    assert exc.value.largest[0][0] == "out"
    assert "out" in str(exc.value) and "paths" in str(exc.value)


def test_byte_cap_stops_the_walk(hermes_profile, monkeypatch):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "dump.json").write_bytes(b"{" * 4096)
    monkeypatch.setattr(agent_export, "_MAX_EXPORT_BYTES", 1024)

    with pytest.raises(agent_export.ExportTooLargeError) as exc:
        agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)
    assert exc.value.byte_count > 1024


def test_cap_applies_to_explicit_paths_too(hermes_profile, monkeypatch):
    seller_id, agent_id, agent_dir = hermes_profile
    (agent_dir / "dump.json").write_bytes(b"{" * 4096)
    monkeypatch.setattr(agent_export, "_MAX_EXPORT_BYTES", 1024)
    with pytest.raises(agent_export.ExportTooLargeError):
        agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id, paths=["dump.json"])


# ---------------------------------------------------------------------------
# AgentCore (Hermes) sellers: export reads the S3 profile prefix, not EFS.
# ---------------------------------------------------------------------------


def test_resolve_agent_location_returns_s3_prefix_for_agentcore_owner():
    container = {"owner_id": "owner_1", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    result = agent_export._resolve_agent_location(seller_id="owner_1", agent_id="agent_9", container=container)
    assert result == ("s3", "owner_1/hermes/agent_9")


@pytest.mark.parametrize(
    "seller_id, agent_id",
    [
        ("owner_1", "../../other_owner/hermes/agent_1"),
        ("owner_1", "a/b"),
        ("../owner_2", "agent_9"),
    ],
)
def test_a_traversal_shaped_id_is_refused_and_metered(seller_id, agent_id):
    """``marketplace.path_traversal_attempt`` is the marketplace's last path-traversal
    trip-wire -- observability-stack's P11 alarms on it, and its EFS-side sibling went with
    core/containers/workspace.py. Both halves of the profile key are checked: an unvalidated
    id is how one seller's prefix gets addressed with another's fragment."""
    with patch.object(agent_export, "put_metric") as metric:
        with pytest.raises(agent_export.InvalidAgentIdError):
            agent_export._resolve_agent_location(seller_id=seller_id, agent_id=agent_id)
    metric.assert_called_once_with("marketplace.path_traversal_attempt")


def test_a_good_id_is_never_metered_as_a_traversal():
    """The metric means exactly one thing, so it may only fire on the refusal."""
    with patch.object(agent_export, "put_metric") as metric:
        agent_export._resolve_agent_location(seller_id="owner_1", agent_id="agent_9")
        agent_export.resolve_workspace_agent_id("owner_1", "agent_9")
    metric.assert_not_called()


def test_resolve_workspace_agent_id_meters_its_own_refusal():
    """The router validates through THIS seam before the Friday guard and the routine
    capture, so it is a second entry point the trip-wire has to cover."""
    with patch.object(agent_export, "put_metric") as metric:
        with pytest.raises(agent_export.InvalidAgentIdError):
            agent_export.resolve_workspace_agent_id("owner_1", "../escape")
    metric.assert_called_once_with("marketplace.path_traversal_attempt")


@pytest.fixture
def s3_hermes_bucket():
    """Moto-backed S3 bucket wired in as settings.OWNER_STATE_BUCKET -- the
    profile writer's (hermes_profiles.upload_profile) single source of truth
    for this bucket, the same one the Hermes bridge's sync.py uploads owner
    profiles into (<owner>/hermes/<agent_id>/...)."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-replicas")
        with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
            yield client


def test_download_s3_profile_excludes_runtime_and_secret_keys(s3_hermes_bucket, tmp_path):
    prefix = "owner_1/hermes/agent_1"
    keep = {
        "SOUL.md": b"# Soul",
        "skills/foo.md": b"# Foo skill",
    }
    drop = {
        # Backend-written, owner-keyed config -- the buyer's deploy writes its own.
        "config.yaml": b"model: ${ANTHROPIC_MODEL}",
        "lightbulb-facts.json": b"{}",
        "cli-env.json": b"{}",
        # Connector credential projections and the agent's $HOME.
        "home/.aws/credentials": b"[default]",
        "home/quickbooks/token.json": b"{}",
        "skills/shopify/token.sh": b"#!/bin/sh",
        ".env": b"SECRET=1",
        ".revision": b"abc123",
        "sessions/session-1.json": b"{}",
        "sessions/request_dump_20260101.json": b"{}",
        "logs/app.log": b"boot ok",
        "mcp-tokens/stripe.json": b'{"token": "x"}',
        # Hermes's learned memory -- USER.md is the seller's own profile (and the pasted
        # business-context dump lands there), so it never ships to a buyer.
        "memories/USER.md": b"Owner runs a bakery",
        "memories/MEMORY.md": b"agent notes",
        "memories/user_profile_overflow.txt": b"more owner context",
        # A retired issue -> room binding: the seller's own room and issue ids.
        "lightbulb-issue-bindings/i-1.json": b'{"sessionKey": "agent:a:lightbulb:channel:x"}',
        # Hermes's session store: every room/mail/call/Paperclip transcript.
        "state.db": b"SQLite format 3",
        "state.db-wal": b"wal",
        "state.db-shm": b"shm",
    }
    for key, body in {**keep, **drop}.items():
        s3_hermes_bucket.put_object(Bucket="test-replicas", Key=f"{prefix}/{key}", Body=body)

    dest = tmp_path / "profile"
    dest.mkdir()
    agent_export._download_s3_profile(prefix, dest)

    downloaded = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    assert downloaded == set(keep.keys())


def test_download_s3_profile_refuses_an_agent_with_no_profile(s3_hermes_bucket, tmp_path):
    """A valid id with nothing under its prefix must not publish an empty listing."""
    with pytest.raises(agent_export.AgentNotFoundError):
        agent_export._download_s3_profile("owner_1/hermes/gone", tmp_path)


def _seed_publishable_profile(s3, prefix: str, agent_id: str) -> None:
    """The minimum a profile needs to be publishable: content, the roster openclaw.json the
    slice is projected from, plus the two things the download must REFUSE to carry."""
    s3.put_object(Bucket="test-replicas", Key=f"{prefix}/SOUL.md", Body=b"# Soul")
    s3.put_object(
        Bucket="test-replicas",
        Key=f"{prefix}/openclaw.json",
        Body=json.dumps({"agents": {"list": [{"id": agent_id, "name": "MyAgent"}]}}).encode(),
    )
    s3.put_object(Bucket="test-replicas", Key=f"{prefix}/.env", Body=b"SECRET=1")
    s3.put_object(Bucket="test-replicas", Key=f"{prefix}/sessions/x.json", Body=b"{}")


def test_export_agent_from_profile_reads_the_s3_profile_and_its_exclusions(fake_efs, s3_hermes_bucket):
    """The export reads the seller's Hermes S3 profile. The ``fake_efs`` tree is NOT mirrored
    by this fixture, so README.md/scripts/run.sh -- which exist only on disk -- prove the
    export never reaches for anything outside the profile; ``.env`` and ``sessions/`` prove
    the download's own exclusions hold."""
    seller_id, agent_id, _agent_dir = fake_efs
    prefix = f"{seller_id}/hermes/{agent_id}"
    _seed_publishable_profile(s3_hermes_bucket, prefix, agent_id)

    pkg = agent_export.export_agent_from_profile(seller_id=seller_id, agent_id=agent_id)

    names = _list_tar_names(pkg.tarball_bytes)
    assert any("SOUL.md" in n for n in names)
    assert not any("README.md" in n for n in names)
    assert not any(".env" in n for n in names)
    assert not any("sessions" in n for n in names)
    assert pkg.manifest["agent_id"] == agent_id


def test_enumerate_for_preview_reads_the_s3_profile(fake_efs, s3_hermes_bucket):
    seller_id, agent_id, _agent_dir = fake_efs
    _seed_publishable_profile(s3_hermes_bucket, f"{seller_id}/hermes/{agent_id}", agent_id)

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)

    assert preview["agent_id"] == agent_id
    assert "SOUL.md" in preview["persona_files"]


# ---------------------------------------------------------------------------
# Fix round 1, item 3: a raising S3 downloader must not leak the scratch dir.
# ---------------------------------------------------------------------------


def test_export_agent_from_profile_cleans_up_scratch_dir_when_download_raises(fake_efs, monkeypatch):
    seller_id, agent_id, _agent_dir = fake_efs
    captured: list[Path] = []

    def _raising_download(prefix, dest):
        captured.append(dest)
        assert dest.exists()  # the scratch dir was created before the raise
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(agent_export, "_download_s3_profile", _raising_download)

    with pytest.raises(RuntimeError, match="s3 unavailable"):
        agent_export.export_agent_from_profile(
            seller_id=seller_id, agent_id=agent_id, container={"runtime_host": "agentcore"}
        )

    assert captured, "the downloader was never called"
    assert not captured[0].exists(), "the scratch dir leaked after a raising download"


def test_enumerate_for_preview_cleans_up_scratch_dir_when_download_raises(fake_efs, monkeypatch):
    seller_id, agent_id, _agent_dir = fake_efs
    captured: list[Path] = []

    def _raising_download(prefix, dest):
        captured.append(dest)
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(agent_export, "_download_s3_profile", _raising_download)

    with pytest.raises(RuntimeError, match="s3 unavailable"):
        agent_export.enumerate_for_preview(
            seller_id=seller_id, agent_id=agent_id, container={"runtime_host": "agentcore"}
        )

    assert captured and not captured[0].exists()
