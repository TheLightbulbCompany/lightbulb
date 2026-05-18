"""Tests for agent_export — Path B (snapshot agent from EFS)."""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import agent_export  # noqa: E402


@pytest.fixture
def fake_efs(tmp_path: Path):
    """Builds /tmp/<root>/users/<seller>/workspaces/<agent-id>/ with sample
    files and patches get_workspace() to return a Workspace anchored at
    <root>.

    Layout mirrors the real OpenClaw EFS convention:
      agents/{id}/      → OpenClaw internal state (sessions/, agent/auth)
      workspaces/{id}/  → seller-facing content (persona files, skills,
                          per-agent openclaw.json) — what gets exported.

    Also writes the seller's per-user openclaw.json with an agents.list[]
    entry for ``agent_id`` so ``export_agent_from_efs`` can synthesize a
    real openclaw_slice (matching the admin-catalog publish behavior).
    """
    seller_id = "user_seller_abc"
    agent_id = "my-agent-001"

    user_root = tmp_path / "users" / seller_id
    agent_dir = user_root / "workspaces" / agent_id
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
                            "workspace": ".openclaw/workspaces/old-id",
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


def test_export_skips_junk_dirs(fake_efs):
    seller_id, agent_id, _ = fake_efs
    pkg = agent_export.export_agent_from_efs(seller_id=seller_id, agent_id=agent_id)
    names = _list_tar_names(pkg.tarball_bytes)
    assert not any("__pycache__" in n for n in names)
    assert not any(".git" in n for n in names)


def test_export_ships_workspace_subdirs_named_sessions_or_agent(fake_efs):
    """Regression for Codex P2 on PR #710 (commit 1f30aa3c): the previous
    implementation stripped any top-level ``sessions/`` or ``agent/`` dir
    from the tarball as a belt-and-suspenders guard against OpenClaw
    runtime-state leaks. That guard is dead now that exports come from
    ``workspaces/{id}/`` (runtime state lives in the sibling ``agents/{id}/``
    tree we don't touch) and worse, it would silently drop legitimate
    seller content in workspaces named ``sessions/`` (e.g. user-recorded
    data) or ``agent/`` (e.g. project files describing the agent).
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
    """Regression for Codex P1 on PR #711 (commit eb8dae83): sellers
    commonly paste credentials into a .txt file (``secret.txt``,
    ``credentials.txt``, ``private-key.txt``). The content scan only
    inspects persona/.md/openclaw.json, so these slip through if .txt
    isn't a config-shape extension. Add .txt to the gate.
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
    """Regression for Codex P1 on PR #711 (commit 43c49a31): the
    AWS-style ``credentials`` file (extensionless, e.g. copied from
    ``~/.aws/credentials``) must be blocked. The original substring-
    based filter caught it; the tighter word-boundary filter
    accidentally let it through because the early-return on
    extensionless files bypassed the token check.

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
    """Regression for Codex P2 on PR #710 (commit 1f30aa3c): the credential
    filename filter used to substring-match the basename, so a skill named
    ``secretary.md`` or ``secret-santa.md`` or a helper file
    ``credentials-helper.ts`` would be silently dropped — yet
    ``_enumerate_workspace_skill_names`` would still classify the skill as
    WORKSPACE bucket, leaving the buyer with a config slice referencing a
    file that wasn't actually shipped.

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
    would deliver secrets directly to buyers
    (Codex P1 round 13, commit fc0581bd).
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
    """Regression for the agents/-vs-workspaces/ mix-up (Ray empty tarball,
    PR #710): the OpenClaw EFS layout puts INTERNAL runtime state under
    ``agents/{id}/`` and the actual seller workspace content under
    ``workspaces/{id}/``. The export must tar the workspace dir; the agents
    dir holds only sessions/ logs and OAuth tokens.

    Plant identical filenames in both dirs and confirm only the workspaces/
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
    workspaces_dir = user_root / "workspaces" / agent_id
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
    e.g. ``/home/node/.openclaw/workspaces/{seller_agent_id}/mcp-resy/index.js``.
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
                "args": [f"/home/node/.openclaw/workspaces/{agent_id}/mcp-resy/index.js"],
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
    orphan_dir = tmp_path / "users" / seller_id / "workspaces" / "orphan-agent-99"
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
async def test_is_reserved_friday_agent_false_when_no_paperclip_agents():
    """Paperclip unreachable / not-yet-provisioned → empty list → not reserved.

    Graceful degradation: don't block publishes because Paperclip is down.
    """
    fetch = _StubFetch([])
    result = await agent_export.is_reserved_friday_agent(
        owner_id="user_abc",
        agent_id="my-custom-agent",
        fetch_paperclip_agents=fetch,
    )
    assert result is False


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
    """agent.skills referenced in the seller config get bucket-classified
    against the seller's full openclaw.json + workspace contents."""
    seller_id, agent_id, agent_dir = fake_efs
    # Pre-populate seller config: skill `jira` configured in entries,
    # `search` is bundled, `custom-skill` lives in agent workspace.
    user_config = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(user_config.read_text())
    cfg["skills"] = {
        "entries": {"jira": {"apiKey": "secret"}},
        "allowBundled": ["search"],
    }
    # Reflect them into the agent's skills list so they show up in available.
    cfg["agents"]["list"][0]["skills"] = ["jira", "search", "custom-skill"]
    user_config.write_text(json.dumps(cfg))
    # Workspace skill: a file under the agent's workspace skills/ dir.
    skills_dir = agent_dir / "skills"
    skills_dir.mkdir()
    (skills_dir / "custom-skill.md").write_text("# custom skill\n", encoding="utf-8")

    preview = agent_export.enumerate_for_preview(seller_id=seller_id, agent_id=agent_id)
    by_name = {s["name"]: s["bucket"] for s in preview["available_skills"]}
    assert by_name["jira"] == "entries"
    assert by_name["search"] == "bundled"
    assert by_name["custom-skill"] == "workspace"


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
