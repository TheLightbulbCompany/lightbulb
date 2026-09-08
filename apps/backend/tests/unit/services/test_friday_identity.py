"""Unit tests for friday_identity.pre_stage_friday_identity."""

import re
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.services import friday_identity

# JS-canonical ISO-8601: exactly 3 fractional digits and a literal ``Z`` —
# what OpenClaw 7.2's workspace-state migration accepts (``new Date(v).
# toISOString() === v``). Python's ``.isoformat()`` (6-digit micros + ``+00:00``)
# does NOT match this and is rejected by the migration.
_JS_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _assert_js_canonical_roundtrip(ts: str) -> None:
    """The timestamp is JS-canonical AND survives a strptime→strftime round-trip
    byte-identically (3-digit millis preserved, literal ``Z``)."""
    assert _JS_ISO_RE.match(ts), f"{ts!r} is not JS-canonical (YYYY-MM-DDTHH:MM:SS.mmmZ)"
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    reformatted = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"
    assert reformatted == ts


def _configure_fresh_workspace(ws):
    """seed_workspace_state's one-shot gate checks whether the agent workspace
    dir already exists; fresh (this provision creates it) → the state file is
    seeded, so tests asserting the historical 6-write shape stay valid."""
    ws.agent_workspace_path.return_value.is_dir.return_value = False
    return ws


def _configure_existing_workspace(ws, *, stale_state_file=False, shared_db=False):
    """Existing workspace (reprovision): the state file is never re-written.
    ``stale_state_file`` / ``shared_db`` drive the heal branch — a stale copy
    is deleted only when the shared state DB proves a doctor pass has run."""
    wsdir = ws.agent_workspace_path.return_value
    wsdir.is_dir.return_value = True
    # (workspace_dir / ".openclaw" / "workspace-state.json").is_file()
    wsdir.__truediv__.return_value.__truediv__.return_value.is_file.return_value = stale_state_file
    # (user_path(owner) / "state" / "openclaw.sqlite").is_file()
    ws.user_path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = shared_db
    return ws


@pytest.mark.asyncio
async def test_skipped_when_no_paperclip_agents():
    """Empty list → seeder returns early without touching workspace."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[],
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_skipped_when_no_ceo_in_list():
    """Agents present but none with role='ceo' → seeder skips."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "uuid-1", "role": "engineer"},
            {"id": "uuid-2", "role": "designer"},
        ],
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_personal_owner_writes_four_files(monkeypatch):
    """Personal owner (user_*) → all four files written with personal content."""
    workspace = _configure_fresh_workspace(MagicMock())
    # write_file_if_missing returns True (file did not exist).
    workspace.write_file_if_missing = MagicMock(return_value=True)

    # Stub profile resolution: tests don't depend on Clerk in this task.
    fake_profile = {
        "kind": "personal",
        "owner_display": "Sam",
        "first_name_or_fallback": "Sam",
        "full_name": "Sam Rivers",
        "email": "sam@example.com",
    }

    async def fake_resolve(owner_id):
        return fake_profile

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "role": "ceo", "name": "Friday"},
        ],
        workspace=workspace,
    )

    # Six files written under the right path: 5 markdown templates + the
    # .openclaw/isol8-setup-seed.json sentinel that prevents OpenClaw's
    # boot-time reconcile from treating the customized persona files as a
    # stale, uncompleted setup wizard.
    assert workspace.write_file_if_missing.call_count == 6
    paths = [call.args[1] for call in workspace.write_file_if_missing.call_args_list]
    assert all(p.startswith("workspace-f47ac10b-58cc-4372-a567-0e02b2c3d479/") for p in paths)
    assert sorted(p.rsplit("/", 1)[-1] for p in paths) == [
        "AGENTS.md",
        "IDENTITY.md",
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
        "isol8-setup-seed.json",
    ]

    # Content interpolation works.
    contents_by_name = {
        call.args[1].rsplit("/", 1)[-1]: call.args[2] for call in workspace.write_file_if_missing.call_args_list
    }
    assert "Friday" in contents_by_name["IDENTITY.md"]
    assert "Sam's AI company" in contents_by_name["SOUL.md"]
    assert "sam@example.com" in contents_by_name["USER.md"]

    # No BOOTSTRAP.md — deleted (spec PR-6, BOOTSTRAP): OpenClaw's boot-time
    # reconcile never surfaces a seeded BOOTSTRAP.md into Friday's context
    # (verified inert against the pinned fork), so the file was written but
    # never read. The kickoff is a server-dispatched chat.send instead.
    assert "BOOTSTRAP.md" not in contents_by_name

    # MEMORY.md is seeded as a structured durable-memory scaffold so every
    # Friday starts with a place to record decisions / preferences / facts.
    memory_md = contents_by_name["MEMORY.md"]
    assert "# MEMORY.md" in memory_md
    assert "## About Sam" in memory_md
    assert "## Operating Decisions" in memory_md
    assert "## Known Issues & Workarounds" in memory_md
    assert "## Lessons Learned" in memory_md

    # AGENTS.md is the orchestration playbook. Friday's coordination now goes
    # through the bundled `paperclip` skill (curl-based REST), NOT the MCP — so
    # the MCP server and its camelCase tool names must be gone.
    agents_md = contents_by_name["AGENTS.md"]
    # MCP is removed entirely.
    assert "MCP" not in agents_md
    assert "paperclipCreateIssue" not in agents_md
    assert "paperclipListAgents" not in agents_md
    assert "@paperclipai/mcp-server" not in agents_md
    # The bundled skill is named, plus the REST surface it wraps.
    assert "`paperclip` skill" in agents_md
    assert "/api/issues" in agents_md
    # Per-agent identity arrives via the paperclip-exec-creds plugin's
    # resolve_exec_env hook, so the playbook names the vars but must NEVER
    # tell the agent to read the key file — that instruction is what had a
    # live token printed into the owner's transcript.
    assert "paperclip-claimed-api-key.json" not in agents_md
    assert "jq -r .token" not in agents_md
    assert "PAPERCLIP_AGENT_ID" in agents_md
    assert "X-Paperclip-Run-Id" in agents_md
    # Heartbeat doctrine from the paperclip skill.
    assert "PAPERCLIP_WAKE_REASON" in agents_md
    assert "PAPERCLIP_RUN_ID" in agents_md
    assert "409" in agents_md  # 'never retry a 409' rule
    # Plan-document / work-product doctrine: a plan is a plan document (not a
    # file), a deliverable is a work product, and every issue gets a real
    # disposition rather than a lingering in_progress + comment.
    assert "documents/plan" in agents_md
    assert "work product" in agents_md
    assert "request_confirmation" in agents_md
    assert "in_progress" in agents_md
    # Only Atlas (recruiter) and Friday (CEO) ship in the container, so the
    # playbook may name Atlas as the hire path. Every other specialist must
    # come from paperclipListAgents at runtime — no hardcoded roster.
    assert "Atlas" in agents_md
    assert "Pulse" not in agents_md
    assert "Tally" not in agents_md
    # Owner display interpolated.
    assert "Sam's AI company" in agents_md or "Sam" in agents_md
    # Triage section present — handles bubble-ups from stuck specialists.
    assert "triage" in agents_md.lower() or "bubble-up" in agents_md.lower()


@pytest.mark.asyncio
async def test_org_owner_uses_org_template(monkeypatch):
    """Org owner (org_*) → USER.md uses org variant referencing the org name."""
    workspace = _configure_fresh_workspace(MagicMock())
    workspace.write_file_if_missing = MagicMock(return_value=True)

    fake_profile = {
        "kind": "org",
        "owner_display": "Acme Inc",
        "first_name_or_fallback": "team",
        "org_name": "Acme Inc",
        "owner_email": "founder@acme.com",
    }

    async def fake_resolve(owner_id):
        return fake_profile

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    await friday_identity.pre_stage_friday_identity(
        owner_id="org_xyz",
        paperclip_agents=[{"id": "uuid-org-friday", "role": "ceo", "name": "Friday"}],
        workspace=workspace,
    )

    contents_by_name = {
        call.args[1].rsplit("/", 1)[-1]: call.args[2] for call in workspace.write_file_if_missing.call_args_list
    }
    # Org USER.md mentions the org and the owner email.
    user_md = contents_by_name["USER.md"]
    assert "Acme Inc" in user_md
    assert "founder@acme.com" in user_md
    # Sanity: this should NOT have personal-template fields.
    assert "What to call them" not in user_md


@pytest.mark.asyncio
async def test_skipped_existing_when_files_already_present(monkeypatch):
    """Reprovision case: existing workspace → the state file is NOT
    re-attempted (one-shot, #1429 — a re-write into a migrated workspace
    bricks every agent run), and write_file_if_missing returning False on the
    templates records skipped_existing without error."""
    workspace = _configure_existing_workspace(MagicMock())
    # All template writes return False (files already exist on EFS).
    workspace.write_file_if_missing = MagicMock(return_value=False)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    # Should not raise.
    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    # Only the 5 markdown templates are attempted — the state file is one-shot
    # and an existing workspace never gets it re-written (nor deleted, absent
    # a stale copy + a migrated state DB).
    assert workspace.write_file_if_missing.call_count == 5
    attempted = [c.args[1] for c in workspace.write_file_if_missing.call_args_list]
    assert not any(p.endswith("workspace-state.json") for p in attempted)
    workspace.delete_file.assert_not_called()


@pytest.mark.asyncio
async def test_state_file_written_first_with_setup_completed(monkeypatch):
    """The native setup-state sentinel (.openclaw/isol8-setup-seed.json):
    - is the FIRST write (so a partial template-write failure still leaves
      OpenClaw in setup-completed state)
    - lives under .openclaw/ at the right path
    - contains setupCompletedAt (the field upstream's reconcile checks)
    - carries NO legacy ``version`` key — the fork's boot hook feeds it to
      ``mergeWorkspaceSetupState``, which owns the row version itself
    """
    import json as _json

    workspace = _configure_fresh_workspace(MagicMock())
    workspace.write_file_if_missing = MagicMock(return_value=True)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    calls = workspace.write_file_if_missing.call_args_list
    # First write is the state file (defensive ordering).
    first_path = calls[0].args[1]
    first_content = calls[0].args[2]
    assert first_path == "workspace-uuid-1/.openclaw/isol8-setup-seed.json"

    state = _json.loads(first_content)
    assert "version" not in state
    # setupCompletedAt is the field reconcileWorkspaceBootstrapCompletionState
    # checks at upstream src/agents/workspace.ts — must be non-empty.
    assert isinstance(state["setupCompletedAt"], str)
    assert state["setupCompletedAt"]
    assert isinstance(state["bootstrapSeededAt"], str)
    assert state["bootstrapSeededAt"]


def test_js_iso_now_is_js_canonical_and_roundtrips():
    """The shared helper emits ``YYYY-MM-DDTHH:MM:SS.mmmZ`` — byte-identical to
    JS ``Date.prototype.toISOString`` — so OpenClaw 7.2's workspace-state
    migration (``new Date(v).toISOString() === v``) accepts it."""
    _assert_js_canonical_roundtrip(friday_identity.js_iso_now())


@pytest.mark.asyncio
async def test_state_file_timestamps_are_js_canonical(monkeypatch):
    """Friday's seeded workspace-state.json timestamps must be JS-canonical.

    OpenClaw 7.2 migrates this legacy file into its SQLite state store and
    validates each timestamp with JS ``new Date(v).toISOString() === v`` —
    exactly 3 fractional digits + literal ``Z``. Python's ``.isoformat()``
    (6-digit micros + ``+00:00``) fails that round-trip: the boot doctor logs
    ``Failed reading legacy workspace state: legacy workspace setup bootstrap
    timestamp is invalid``, the migration aborts, and every agent run then
    throws ``Legacy workspace setup state requires migration``.
    """
    import json as _json

    workspace = _configure_fresh_workspace(MagicMock())
    workspace.write_file_if_missing = MagicMock(return_value=True)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    state = _json.loads(workspace.write_file_if_missing.call_args_list[0].args[2])
    _assert_js_canonical_roundtrip(state["bootstrapSeededAt"])
    _assert_js_canonical_roundtrip(state["setupCompletedAt"])


@pytest.mark.asyncio
async def test_state_file_failure_does_not_block_template_writes(monkeypatch):
    """If workspace-state.json write raises, the six markdown templates
    still write (best-effort: state file is the floor, not the ceiling)."""
    workspace = _configure_fresh_workspace(MagicMock())

    # First call (state file) raises; subsequent calls (5 templates) succeed.
    side_effects = [OSError("disk full")] + [True] * 5
    workspace.write_file_if_missing = MagicMock(side_effect=side_effects)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    # Should not raise.
    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    # 1 state-file attempt (failed) + 5 template writes = 6 calls.
    assert workspace.write_file_if_missing.call_count == 6
    template_names = sorted(c.args[1].rsplit("/", 1)[-1] for c in workspace.write_file_if_missing.call_args_list[1:])
    assert template_names == ["AGENTS.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "USER.md"]


# ---------------------------------------------------------------------------
# seed_workspace_state — one-shot semantics + stale-copy heal (#1429)
# ---------------------------------------------------------------------------
#
# Verified against the deployed fork tag v2026.7.2-isol8.1: the runtime gate
# (assertNoUnmigratedWorkspaceState) is PRESENCE-ONLY — if the file exists,
# every agent run throws. First boot's doctor imports it into the shared
# SQLite store, records a sha256 receipt, and deletes the file; a re-seed
# (fresh timestamp → new sha) makes every later doctor pass refuse cleanup
# ("retired source now conflicts") — a permanent brick. Exercised on a REAL
# Workspace over tmp_path: the one-shot gate is filesystem-shape-sensitive
# and a MagicMock would happily pass states the fix exists to distinguish.


@pytest.fixture
def real_workspace(tmp_path, monkeypatch):
    from core.containers.workspace import Workspace

    # Skip the EFS access-point chown (uid 1000) — not permitted on dev boxes.
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "local")
    return Workspace(mount_path=str(tmp_path))


def test_seed_workspace_state_seeds_only_a_fresh_workspace(real_workspace, tmp_path):
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    legacy = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "workspace-state.json"

    # Fresh workspace: seeded, with the JS-canonical timestamp contract.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"
    import json as _json

    payload = _json.loads(state.read_text())
    # The NATIVE sentinel carries no legacy ``version`` key, and the legacy file
    # is never written -- so the #1429 presence gate has nothing to trip on.
    assert set(payload.keys()) == {"bootstrapSeededAt", "setupCompletedAt"}
    assert not legacy.exists()
    _assert_js_canonical_roundtrip(payload["setupCompletedAt"])
    _assert_js_canonical_roundtrip(payload["bootstrapSeededAt"])

    # Re-run before any boot (retried provision): the workspace exists and the
    # original seed is preserved byte-for-byte — it migrates cleanly on boot #1.
    original = state.read_text()
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert state.read_text() == original


def test_seed_workspace_state_never_rewrites_after_migration(real_workspace, tmp_path):
    """THE #1429 fix: boot #1's doctor migrated the file into SQLite and
    deleted it. A reprovision (the trial->paid conversion path) must NOT
    re-write it — the re-seeded copy's sha no longer matches the migration
    receipt, doctor refuses cleanup forever, and the presence-only runtime
    gate bricks every agent run."""
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"

    # Boot #1: the fork's hook converts + deletes the sentinel (and the shared
    # DB exists once OpenClaw has booted).
    state.unlink()
    (tmp_path / "user_x" / "state").mkdir()
    (tmp_path / "user_x" / "state" / "openclaw.sqlite").write_bytes(b"")

    # Reprovision: the old write_file_if_missing found the path empty and
    # re-wrote it. One-shot must skip instead.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert not state.exists()


def test_seed_workspace_state_heals_an_already_bricked_workspace(real_workspace, tmp_path):
    """Pre-fix cohort: the file was re-seeded into an already-migrated
    workspace (shared state DB present). The stale copy's only runtime effect
    is tripping the presence gate — delete it."""
    # A container seeded BEFORE the native-sentinel cutover carries the legacy
    # file; we no longer write it, so plant it the way that cohort has it.
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "workspace-state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"version": 1, "bootstrapSeededAt": "x", "setupCompletedAt": "x"}')
    (tmp_path / "user_x" / "state").mkdir()
    (tmp_path / "user_x" / "state" / "openclaw.sqlite").write_bytes(b"")

    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "deleted_stale"
    assert not state.exists()

    # And the heal is itself one-shot: the next reprovision is a plain skip.
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"


def test_seed_workspace_state_keeps_an_unmigrated_seed(real_workspace, tmp_path):
    """No shared state DB == the container never booted since seeding: the
    file is the original un-migrated seed and must be left for boot #1's
    doctor to import (deleting it would only lose the setup-completed marker)."""
    state = tmp_path / "user_x" / "workspace-uuid-f" / ".openclaw" / "isol8-setup-seed.json"
    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "seeded"

    assert friday_identity.seed_workspace_state(real_workspace, "user_x", "uuid-f") == "skipped"
    assert state.is_file()


@pytest.mark.asyncio
async def test_pre_stage_heals_stale_state_file_on_existing_workspace(monkeypatch):
    """Call-site wiring: an existing workspace with a stale re-seeded state
    file AND a migrated shared DB gets the file deleted (never re-written)."""
    workspace = _configure_existing_workspace(MagicMock(), stale_state_file=True, shared_db=True)
    workspace.write_file_if_missing = MagicMock(return_value=False)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    workspace.delete_file.assert_called_once_with("user_abc", "workspace-uuid-1/.openclaw/workspace-state.json")
    attempted = [c.args[1] for c in workspace.write_file_if_missing.call_args_list]
    assert not any(p.endswith("workspace-state.json") for p in attempted)


@pytest.mark.asyncio
async def test_resolve_personal_owner_uses_clerk_get_user(monkeypatch):
    """Personal owner_id → reads first_name/last_name/email from clerk_admin."""
    from core.services import clerk_admin

    async def fake_get_user(user_id):
        assert user_id == "user_abc"
        return {
            "first_name": "Sam",
            "last_name": "Rivers",
            "email_addresses": [{"email_address": "sam@example.com"}],
            "primary_email_address_id": None,
        }

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["kind"] == "personal"
    assert profile["full_name"] == "Sam Rivers"
    assert profile["first_name_or_fallback"] == "Sam"
    assert profile["owner_display"] == "Sam"
    assert profile["email"] == "sam@example.com"


@pytest.mark.asyncio
async def test_resolve_org_owner_uses_clerk_get_organization(monkeypatch):
    """Org owner_id → reads org name from clerk_admin.get_organization
    and owner email from paperclip_owner_email."""
    from core.services import clerk_admin
    from core.services import paperclip_owner_email

    async def fake_get_org(org_id):
        assert org_id == "org_xyz"
        return {"name": "Acme Inc", "created_by": "user_founder_123"}

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        assert org_id == "org_xyz"
        assert fallback_user_id == "user_founder_123"
        return "founder@acme.com"

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)

    profile = await friday_identity._resolve_owner_profile("org_xyz")
    assert profile["kind"] == "org"
    assert profile["org_name"] == "Acme Inc"
    assert profile["owner_email"] == "founder@acme.com"
    assert profile["owner_display"] == "Acme Inc"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_minimal_when_clerk_fails(monkeypatch):
    """clerk_admin.get_user returns None → resolver still returns a usable
    profile with email='(unknown)'."""
    from core.services import clerk_admin

    async def fake_get_user_returns_none(user_id):
        return None

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user_returns_none)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["kind"] == "personal"
    assert profile["email"] == "(unknown)"
    assert profile["full_name"] == "(unknown)"


@pytest.mark.asyncio
async def test_resolve_personal_owner_falls_back_to_email_local_part(monkeypatch):
    """No first_name in Clerk → first_name_or_fallback derives from email local-part."""
    from core.services import clerk_admin

    async def fake_get_user(user_id):
        return {
            "first_name": None,
            "last_name": None,
            "email_addresses": [{"id": "ea_1", "email_address": "tao@example.com"}],
            "primary_email_address_id": "ea_1",
        }

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["first_name_or_fallback"] == "tao"
    assert profile["owner_display"] == "tao"
    assert profile["full_name"] == "(unknown)"
    assert profile["email"] == "tao@example.com"


@pytest.mark.asyncio
async def test_resolve_org_owner_falls_back_when_clerk_returns_no_org(monkeypatch):
    """clerk_admin.get_organization returns None → org_name falls back to 'your team'."""
    from core.services import clerk_admin
    from core.services import paperclip_owner_email

    async def fake_get_org(org_id):
        return None

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        # fallback_user_id should be None when org has no created_by.
        assert fallback_user_id is None
        return None

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)

    profile = await friday_identity._resolve_owner_profile("org_xyz")
    assert profile["kind"] == "org"
    assert profile["org_name"] == "your team"
    assert profile["owner_email"] == "(unknown)"
    assert profile["owner_display"] == "your team"


# ---------------------------------------------------------------------------
# pre_stage_paperclip_identities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paperclip_identities_writes_per_agent_file():
    """Each agent with a minted key gets its own claimed-key file in its own
    workspace with the exact Isol8-defined shape."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo", "name": "Friday"},
            {"id": "agent-atlas", "role": "general", "name": "Atlas"},
        ],
        agent_keys={"agent-friday": "key-f", "agent-atlas": "key-a"},
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    calls = {c.args[1]: c.args[2] for c in workspace.write_file_if_missing.call_args_list}
    assert set(calls) == {
        "workspace-agent-friday/paperclip-claimed-api-key.json",
        "workspace-agent-atlas/paperclip-claimed-api-key.json",
    }
    friday_doc = _json.loads(calls["workspace-agent-friday/paperclip-claimed-api-key.json"])
    assert friday_doc == {
        "token": "key-f",
        "agentId": "agent-friday",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }
    atlas_doc = _json.loads(calls["workspace-agent-atlas/paperclip-claimed-api-key.json"])
    assert atlas_doc["token"] == "key-a"
    assert atlas_doc["agentId"] == "agent-atlas"


@pytest.mark.asyncio
async def test_paperclip_identities_skips_agents_without_a_minted_key():
    """An agent whose key mint failed (absent from agent_keys) gets no file."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo"},
            {"id": "agent-nokey", "role": "general"},
        ],
        agent_keys={"agent-friday": "key-f"},  # agent-nokey missing
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    paths = [c.args[1] for c in workspace.write_file_if_missing.call_args_list]
    assert paths == ["workspace-agent-friday/paperclip-claimed-api-key.json"]


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_keys():
    """No minted keys → nothing staged (Paperclip unreachable at provision)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_company_or_url():
    """Missing company_id or api_url → skip entirely (incomplete context)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id=None,
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_per_file_failure_does_not_block_rest():
    """One agent's write raising must not stop the others (best-effort)."""
    workspace = MagicMock()
    # First write raises, second succeeds.
    workspace.write_file_if_missing = MagicMock(side_effect=[OSError("disk full"), True])

    # Should not raise.
    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-a", "role": "ceo"},
            {"id": "agent-b", "role": "general"},
        ],
        agent_keys={"agent-a": "ka", "agent-b": "kb"},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# stage_paperclip_identity_for_agent (single-agent, post-create path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_single_agent_writes_identity_file():
    """The single-agent seeder writes one claimed-key file with the exact
    Isol8-defined shape into the agent's own workspace."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key-live",
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_called_once()
    rel_path = workspace.write_file_if_missing.call_args.args[1]
    assert rel_path == "workspace-agent-live/paperclip-claimed-api-key.json"
    doc = _json.loads(workspace.write_file_if_missing.call_args.args[2])
    assert doc == {
        "token": "key-live",
        "agentId": "agent-live",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,company_id,api_url",
    [
        ("", "comp-1", "http://pc:3100"),  # key mint failed
        ("key", None, "http://pc:3100"),  # company unresolved
        ("key", "comp-1", None),  # paperclip url unset
    ],
)
async def test_stage_single_agent_skips_on_incomplete_context(token, company_id, api_url):
    """A missing token/company_id/api_url skips staging (no file written);
    the next reprovision self-heals."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token=token,
        company_id=company_id,
        api_url=api_url,
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_stage_single_agent_write_failure_is_swallowed():
    """A write error must not raise — best-effort, the next reprovision retries."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(side_effect=OSError("disk full"))

    # Should not raise.
    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key",
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    # The single write was attempted (and its error swallowed).
    workspace.write_file_if_missing.assert_called_once()


# ---------------------------------------------------------------------------
# _migrate_legacy_user_md
# ---------------------------------------------------------------------------

_LEGACY_USER_MD = """\
# USER.md - About Your Human

- **Name:** Sam Rivers
- **What to call them:** Sam
- **Email:** sam@example.com
- **Isol8 plan:** Starter ($50/mo flat-fee)
- **Provider:** Bedrock (Claude, Isol8-managed credits)

## Context

Sam is building a health-tech startup. Prefers direct answers.
"""


class _MigrationWs:
    def __init__(self, content):
        self.content = content
        self.written = None

    def read_file(self, user_id, path):
        if self.content is None:
            raise FileNotFoundError(path)
        return self.content

    def write_file(self, user_id, path, content):
        self.written = (path, content)
        # A write is visible to the next read, as it is on EFS. Without this
        # the fake silently breaks migration CHAINING: several migrations touch
        # AGENTS.md, and if each re-reads the original the last writer wins and
        # discards the others. Real Workspace.read_file goes to the filesystem.
        self.content = content


def test_migrate_legacy_user_md_swaps_lines_and_preserves_content():
    ws = _MigrationWs(_LEGACY_USER_MD)
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is True
    path, content = ws.written
    assert path == "workspace-uuid-f/USER.md"
    assert "lightbulb-facts.json" in content
    assert "Isol8 plan" not in content
    assert "- **Provider:**" not in content
    # Friday's evolved content survives untouched.
    assert "health-tech startup" in content
    assert "- **Name:** Sam Rivers" in content


def test_migrate_legacy_user_md_noop_on_new_template():
    ws = _MigrationWs("# USER.md\n- **Plan & provider:** read `~/.openclaw/lightbulb-facts.json`\n")
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is False
    assert ws.written is None


def test_migrate_legacy_user_md_noop_when_missing():
    ws = _MigrationWs(None)
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is False


def test_migrate_legacy_user_md_preserves_owner_edited_provider_line():
    edited = _LEGACY_USER_MD.replace(
        "- **Provider:** Bedrock (Claude, Isol8-managed credits)",
        "- **Provider:** my own custom note about providers",
    )
    ws = _MigrationWs(edited)
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is True
    _, content = ws.written
    # The plan line migrated, but the owner-edited provider line stays.
    assert "my own custom note about providers" in content
    assert "lightbulb-facts.json" in content


def test_migrate_legacy_user_md_noop_when_plan_line_is_substring_only():
    """The legacy line quoted mid-prose must not trigger a spurious write."""
    ws = _MigrationWs("# USER.md\nSam once wrote: the seed said - **Isol8 plan:** Starter ($50/mo flat-fee) here.\n")
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is False
    assert ws.written is None


def test_migrate_legacy_user_md_provider_line_needs_plan_anchor():
    """A provider-lookalike line with the plan line only in prose (never as a
    standalone line) must not be dropped — no anchor, no migration."""
    ws = _MigrationWs(
        "# USER.md\n"
        "Note: seed used to say - **Isol8 plan:** Starter ($50/mo flat-fee) inline.\n"
        "- **Provider:** Bedrock (Claude, Isol8-managed credits)\n"
    )
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is False
    assert ws.written is None


def test_migrate_legacy_user_md_only_drops_the_adjacent_provider_line():
    """A legacy-label-shaped provider line in later prose (e.g. Friday's
    Context notes) must survive — only the line immediately after the plan
    line is the seed's, everything else is authored content."""
    content = _LEGACY_USER_MD + (
        "\n## Notes\n"
        "Config history:\n"
        "- **Provider:** Bedrock (Claude, Isol8-managed credits)\n"
        "was what we started with.\n"
    )
    ws = _MigrationWs(content)
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is True
    _, migrated = ws.written
    # The adjacent seed pair migrated...
    assert "lightbulb-facts.json" in migrated
    assert "Isol8 plan" not in migrated
    # ...but the lookalike in the Notes section survives.
    assert migrated.count("- **Provider:** Bedrock (Claude, Isol8-managed credits)") == 1
    assert "Config history:" in migrated


# ---------------------------------------------------------------------------
# Nightly self-directed wake section (issue #1273)
# ---------------------------------------------------------------------------


def _personal_profile():
    return {
        "kind": "personal",
        "owner_display": "Sam",
        "first_name_or_fallback": "Sam",
        "full_name": "Sam Rivers",
        "email": "sam@example.com",
    }


def test_agents_md_contains_company_control_wake_and_dual_condition():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert "## Nightly self-directed wake" in agents_md
    # BOTH server-verified conditions must be present (originKind + marker).
    assert "routine_execution" in agents_md
    assert "[NIGHTLY-CEO-LOOP]" in agents_md
    # The wake advances one company constraint through CEO work. It must not
    # reintroduce the arbitrary-specialist checkout that contradicted the
    # delegation boundary.
    assert "Choose ONE company constraint" in agents_md
    # Renamed to "Delegate SUSTAINED specialist execution": the blanket form
    # banned doing any work at all, which routed one-shot requests to the
    # marketplace. See test_delegation_boundary_is_scoped_to_sustained_work.
    assert "Delegate sustained specialist execution" in agents_md
    assert "delegation is the action" in agents_md
    assert "never checkout" in agents_md
    assert "keep THIS parent non-terminal" in agents_md
    assert "child's completion wakes you" in agents_md
    assert "Pick ONE task you can complete right now" not in agents_md
    assert "NIGHTLY-TASK-TAKEN:" not in agents_md
    assert "Never invent work" in agents_md


# ---------------------------------------------------------------------------
# Webchat capability section — checkout is heartbeat-only; webchat edits and
# assign-to-start need no run id, and run-id mechanics never reach the owner.
# ---------------------------------------------------------------------------


def test_agents_md_contains_webchat_section_after_render():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert "## Webchat sessions (no run id)" in agents_md
    assert "Never attempt checkout or release from webchat" in agents_md
    # The owner never hears about run ids.
    assert 'never tell them you "need a run id"' in agents_md


def test_migrate_add_webchat_section_appends_when_absent():
    ws = _MigrationWs("# AGENTS.md\n\nOld playbook without the section.\n")
    assert friday_identity._migrate_add_webchat_section(ws, "user_abc", "uuid-f") is True
    path, content = ws.written
    assert path == "workspace-uuid-f/AGENTS.md"
    assert content.startswith("# AGENTS.md")
    assert "## Webchat sessions (no run id)" in content
    # The old playbook survives untouched above the appended section.
    assert "Old playbook without the section." in content


def test_migrate_add_webchat_section_noop_when_present():
    rendered = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    ws = _MigrationWs(rendered)
    assert friday_identity._migrate_add_webchat_section(ws, "user_abc", "uuid-f") is False
    assert ws.written is None


def test_migrate_add_webchat_section_noop_when_missing():
    ws = _MigrationWs(None)
    assert friday_identity._migrate_add_webchat_section(ws, "user_abc", "uuid-f") is False


class _HealWs(_MigrationWs):
    """_MigrationWs plus the openclaw.json read the CEO resolver uses."""

    def __init__(self, content, config):
        super().__init__(content)
        self.config = config

    def read_openclaw_config(self, user_id):
        return self.config


def _config_with_agents(*agents):
    """openclaw.json shaped like _build_agents_list writes it."""
    return {
        "agents": {"list": [{"id": agent_id, "params": {"paperclip": {"role": role}}} for agent_id, role in agents]}
    }


@pytest.mark.asyncio
async def test_heal_seeded_files_resolves_the_ceo_by_role_not_position():
    """The reconcile path can leave Atlas ahead of Friday in seeded order, so
    the CEO is resolved by ROLE from openclaw.json. Healing the wrong agent
    would leave Friday stale AND write CEO text into Atlas's workspace."""
    ws = _HealWs(
        "# AGENTS.md\n\nOld playbook without the section.\n",
        _config_with_agents(("uuid-atlas", "general"), ("uuid-friday", "ceo")),
    )

    await friday_identity.heal_seeded_files(owner_id="user_abc", workspace=ws)

    path, content = ws.written
    assert path == "workspace-uuid-friday/AGENTS.md"
    assert "## Webchat sessions (no run id)" in content


class _FleetHealWs:
    """Path-aware EFS fake for the complete public heal chain."""

    def __init__(self, agents_md):
        self.config = _config_with_agents(("uuid-atlas", "general"), ("uuid-friday", "ceo"))
        self.files = {
            "workspace-uuid-friday/AGENTS.md": agents_md,
            "workspace-uuid-friday/USER.md": "# USER.md\n\nOwner-authored facts.\n",
            "workspace-uuid-friday/SOUL.md": "# SOUL.md\n" + friday_identity._SELF_GROUNDING_SECTION,
        }
        self.writes = []

    def read_openclaw_config(self, user_id):
        return self.config

    def read_file(self, user_id, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_file(self, user_id, path, content):
        self.files[path] = content
        self.writes.append((path, content))


@pytest.mark.asyncio
async def test_fleet_heal_rewrites_exact_legacy_nightly_section_once():
    """The image-apply entry point reaches the CEO workspace, performs the
    exact legacy rewrite, preserves surrounding owner content, and is
    idempotent on a later fleet apply."""
    legacy = (
        "# AGENTS.md — Friday's Playbook\n"
        "\nOwner policy before the seeded section.\n"
        + friday_identity._LEGACY_NIGHTLY_WAKE_SECTION
        + friday_identity._WEBCHAT_SECTION
        + "\nOwner policy after the seeded section.\n"
    )
    ws = _FleetHealWs(legacy)

    await friday_identity.heal_seeded_files(owner_id="user_abc", workspace=ws)

    assert [path for path, _ in ws.writes] == ["workspace-uuid-friday/AGENTS.md"]
    healed = ws.files["workspace-uuid-friday/AGENTS.md"]
    assert friday_identity._LEGACY_NIGHTLY_WAKE_SECTION not in healed
    assert friday_identity._NIGHTLY_WAKE_SECTION in healed
    assert "Owner policy before the seeded section." in healed
    assert "Owner policy after the seeded section." in healed
    assert friday_identity._WEBCHAT_SECTION in healed

    await friday_identity.heal_seeded_files(owner_id="user_abc", workspace=ws)
    assert len(ws.writes) == 1


@pytest.mark.asyncio
async def test_heal_seeded_files_skips_when_no_ceo_in_config():
    ws = _HealWs("# AGENTS.md\n", _config_with_agents(("uuid-atlas", "general")))
    await friday_identity.heal_seeded_files(owner_id="user_abc", workspace=ws)
    assert ws.written is None


@pytest.mark.asyncio
async def test_heal_seeded_files_skips_when_config_missing():
    """Legacy pre-Paperclip owners (no config / no agents) have nothing to
    heal — and a missing config must never raise into an image apply."""
    ws = _HealWs("# AGENTS.md\n", None)
    await friday_identity.heal_seeded_files(owner_id="user_abc", workspace=ws)
    assert ws.written is None


def test_agents_md_wake_reason_list_includes_issue_assigned():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert "issue_assigned" in agents_md


def test_agents_md_nightly_limits_self_direction_to_company_control():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    compact = " ".join(agents_md.split())
    assert "This is the ONE wake where you may survey company state" in compact
    assert "not random backlog execution" in compact
    assert "perform a specialist's primary" in compact
    assert "reversible delegation" in compact


def test_soul_md_has_no_nightly_logic():
    """The nightly wake branch must live in AGENTS.md, not SOUL.md — reactive
    behavior in SOUL is unchanged."""
    soul_md = friday_identity._render_templates(_personal_profile())["SOUL.md"]
    assert "NIGHTLY-CEO-LOOP" not in soul_md
    assert "Nightly self-directed wake" not in soul_md


def test_migrate_add_nightly_section_appends_when_absent():
    """An existing AGENTS.md without the section gets it appended; idempotent on
    a second run."""
    store = {"workspace-friday-uuid/AGENTS.md": "# AGENTS.md — Friday's Playbook\n\nold content\n"}

    def read_file(owner_id, rel_path):
        return store[rel_path]

    def write_file(owner_id, rel_path, content):
        store[rel_path] = content

    workspace = MagicMock()
    workspace.read_file = read_file
    workspace.write_file = write_file

    changed = friday_identity._migrate_add_nightly_section(workspace, "user_abc", "friday-uuid")
    assert changed is True
    assert "## Nightly self-directed wake" in store["workspace-friday-uuid/AGENTS.md"]
    assert "old content" in store["workspace-friday-uuid/AGENTS.md"]

    # Second run is a no-op (heading already present).
    changed_again = friday_identity._migrate_add_nightly_section(workspace, "user_abc", "friday-uuid")
    assert changed_again is False


def test_migrate_nightly_section_replaces_only_exact_legacy_block():
    before = (
        "# AGENTS.md — Friday's Playbook\n"
        "\nOwner-authored preface.\n" + friday_identity._LEGACY_NIGHTLY_WAKE_SECTION + "\nOwner-authored footer.\n"
    )
    ws = _MigrationWs(before)

    assert friday_identity._migrate_add_nightly_section(ws, "user_abc", "friday-uuid") is True

    _, healed = ws.written
    assert healed == before.replace(
        friday_identity._LEGACY_NIGHTLY_WAKE_SECTION,
        friday_identity._NIGHTLY_WAKE_SECTION,
        1,
    )
    assert "Owner-authored preface." in healed
    assert "Owner-authored footer." in healed
    assert friday_identity._LEGACY_NIGHTLY_WAKE_SECTION not in healed

    ws.written = None
    assert friday_identity._migrate_add_nightly_section(ws, "user_abc", "friday-uuid") is False
    assert ws.written is None


def test_migrate_nightly_section_preserves_owner_edited_heading_block():
    edited = friday_identity._LEGACY_NIGHTLY_WAKE_SECTION.replace(
        "2. **Review the backlog.**",
        "2. **Review the owner's curated queue.**",
    )
    ws = _MigrationWs("# AGENTS.md\n\nOwner preface.\n" + edited + "\nOwner footer.\n")

    assert friday_identity._migrate_add_nightly_section(ws, "user_abc", "friday-uuid") is False
    assert ws.written is None
    assert "Review the owner's curated queue" in ws.content


def test_migrate_add_nightly_section_noop_when_no_file():
    """No AGENTS.md yet → migration returns False (the seed loop writes the full
    template)."""

    def read_file(owner_id, rel_path):
        raise FileNotFoundError(rel_path)

    workspace = MagicMock()
    workspace.read_file = read_file
    workspace.write_file = MagicMock()

    assert friday_identity._migrate_add_nightly_section(workspace, "user_abc", "friday-uuid") is False
    workspace.write_file.assert_not_called()


# ---------------------------------------------------------------------------
# Platform self-grounding section (SOUL.md)
# ---------------------------------------------------------------------------


def _soul_workspace(store):
    """A Workspace double backed by ``store``, keyed on the relative path."""

    workspace = MagicMock()
    workspace.read_file = lambda owner_id, rel_path: store[rel_path]
    workspace.write_file = lambda owner_id, rel_path, content: store.__setitem__(rel_path, content)
    return workspace


def test_soul_md_contains_self_grounding_section():
    soul_md = friday_identity._render_templates(_personal_profile())["SOUL.md"]
    assert "## You are made of Lightbulb" in soul_md
    assert "skill lightbulb" in soul_md
    # No leftover substitution placeholder in the rendered output.
    assert "{self_grounding_section}" not in soul_md


def test_migrate_add_soul_grounding_appends_when_absent():
    """An existing SOUL.md without the section gets it appended; idempotent on
    a second run."""
    store = {"workspace-friday-uuid/SOUL.md": "# SOUL.md - Who You Are\n\nFriday's own evolved soul.\n"}
    workspace = _soul_workspace(store)

    changed = friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid")
    assert changed is True
    migrated = store["workspace-friday-uuid/SOUL.md"]
    assert "## You are made of Lightbulb" in migrated
    assert "Friday's own evolved soul." in migrated

    changed_again = friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid")
    assert changed_again is False


def test_migrate_add_soul_grounding_rebrands_legacy_heading_in_place():
    """The pre-rebrand section is REWRITTEN, never appended-under.

    Every already-provisioned Friday's SOUL.md carries the old heading. If the
    migration only recognised the new one it would append a second grounding
    section, leaving the stale Isol8-branded one above it forever.
    """
    rel = "workspace-friday-uuid/SOUL.md"
    store = {
        rel: (
            "# SOUL.md - Who You Are\n\nFriday's own evolved soul.\n" + friday_identity._LEGACY_SELF_GROUNDING_SECTION
        )
    }
    workspace = _soul_workspace(store)

    assert friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid") is True
    migrated = store[rel]

    # Rewritten in place: exactly one grounding section, and none of it stale.
    assert migrated.count(friday_identity._SELF_GROUNDING_HEADING) == 1
    assert friday_identity._LEGACY_SELF_GROUNDING_HEADING not in migrated
    assert "made of Isol8" not in migrated
    # Friday's own prose survives, and nothing was appended.
    assert "Friday's own evolved soul." in migrated
    assert migrated.count("## You are made of") == 1

    # Second run is a no-op — the new heading is now the matcher.
    assert friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid") is False
    assert store[rel] == migrated


def test_migrate_add_soul_grounding_rebrands_heading_when_body_was_evolved():
    """A Friday that edited the section's body still gets the heading swapped,
    so the next pass recognises it instead of appending a duplicate."""
    rel = "workspace-friday-uuid/SOUL.md"
    store = {
        rel: (
            "# SOUL.md - Who You Are\n\n"
            + friday_identity._LEGACY_SELF_GROUNDING_HEADING
            + "\n\nI rewrote this part in my own words.\n"
        )
    }
    workspace = _soul_workspace(store)

    assert friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid") is True
    migrated = store[rel]

    assert migrated.count("## You are made of") == 1
    assert friday_identity._LEGACY_SELF_GROUNDING_HEADING not in migrated
    assert "I rewrote this part in my own words." in migrated

    assert friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid") is False


def test_migrate_add_soul_grounding_noop_when_no_file():
    def read_file(owner_id, rel_path):
        raise FileNotFoundError(rel_path)

    workspace = MagicMock()
    workspace.read_file = read_file
    workspace.write_file = MagicMock()

    assert friday_identity._migrate_add_soul_grounding(workspace, "user_abc", "friday-uuid") is False
    workspace.write_file.assert_not_called()


def test_agents_template_checks_marketplace_before_atlas():
    """The playbook must lead with the marketplace, not Atlas.

    A marketplace hire costs $0 inference (tarball extract + config merge) and
    is ready immediately; asking Atlas to build one is a full agent-building
    run against the owner's budget. The cheap option has to be presented first,
    or the model reaches for Atlas by default — observed in prod, where Friday
    routed an engineering hire to Atlas and never mentioned the marketplace.
    """
    body = friday_identity._AGENTS_TEMPLATE
    assert "marketplace_search" in body, "playbook must name the discovery tool"

    # Scope to the hire step itself. Earlier steps mention Atlas descriptively
    # ("new agents ... scaffolded by Atlas show up in the roster"), which is
    # not a recommendation and must not count against the ordering.
    step_3 = body[body.index("3. **Identify or hire.") : body.index("4. **Confirm.")]

    assert "marketplace_search" in step_3, "the hire step must reach for the marketplace"
    assert "Atlas" in step_3, "the hire step must still offer Atlas as the fallback"
    assert step_3.index("marketplace_search") < step_3.index("Atlas"), (
        "marketplace must be presented before Atlas within the hire step"
    )


def test_agents_template_names_the_recommend_tool():
    """Discovery alone is not enough — she needs the tool that renders the
    one-click hire card, or she falls back to pasting a URL."""
    assert "recommend_agents" in friday_identity._AGENTS_TEMPLATE


class _DictWorkspace:
    """EFS stand-in: read_file raises for an unseeded path, like Workspace does."""

    def __init__(self, files: dict[str, str]):
        self.files = dict(files)

    def read_file(self, owner_id: str, rel_path: str) -> str:
        return self.files[rel_path]

    def write_file(self, owner_id: str, rel_path: str, content: str) -> None:
        self.files[rel_path] = content


_UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_retired_skill_id_repaired_when_current_heading_already_present():
    """The stranded case: a Friday healed by the heading-only rebrand swap keeps
    the retired id in its body, and the grounding migration's early return means
    nothing else would ever rewrite it."""
    soul = f"{friday_identity._SELF_GROUNDING_HEADING}\n\nrun `skill isol8` FIRST.\n"
    ws = _DictWorkspace({f"workspace-{_UUID}/SOUL.md": soul})

    assert friday_identity._migrate_retired_skill_id(ws, "owner_1", _UUID) is True
    healed = ws.files[f"workspace-{_UUID}/SOUL.md"]
    assert "skill isol8" not in healed
    assert "skill lightbulb" in healed
    # the heading and surrounding prose are untouched
    assert healed.startswith(friday_identity._SELF_GROUNDING_HEADING)


def test_retired_skill_id_is_a_noop_when_already_current():
    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": "run `skill lightbulb`.\n"})
    assert friday_identity._migrate_retired_skill_id(ws, "owner_1", _UUID) is False


def test_retired_skill_id_repairs_agents_md_too():
    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": "see `skill isol8` for surfaces.\n"})
    assert friday_identity._migrate_retired_skill_id(ws, "owner_1", _UUID) is True
    assert "skill lightbulb" in ws.files[f"workspace-{_UUID}/AGENTS.md"]


def test_paperclip_curl_migrated_to_script():
    """An already-provisioned Friday whose AGENTS.md predates the script keeps
    the hand-typed curl forever (write-if-missing). The migration repoints
    both the fenced example and the sentence describing it, byte-exact."""
    body = (
        "intro prose before it.\n\n"
        + friday_identity._LEGACY_PAPERCLIP_CURL_FENCE
        + "\nmore prose. "
        + friday_identity._LEGACY_PAPERCLIP_AUTH_SENTENCE
        + " tail prose.\n"
    )
    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": body})

    assert friday_identity._migrate_paperclip_curl_to_script(ws, "owner_1", _UUID) is True
    healed = ws.files[f"workspace-{_UUID}/AGENTS.md"]
    assert "Authorization: Bearer $PAPERCLIP_API_KEY" not in healed
    assert "paperclip-api.sh" in healed
    assert friday_identity._PAPERCLIP_AUTH_SENTENCE in healed
    # surrounding prose survives untouched
    assert healed.startswith("intro prose before it.")
    assert healed.endswith(" tail prose.\n")


def test_paperclip_curl_migration_reproduces_the_new_template_byte_exact():
    """The real regression this migration must never introduce: healing an
    old, fully-rendered AGENTS.md must land EXACTLY on what a fresh seed
    produces today, not some approximation of it. Build the pre-fix body by
    reversing the two substitutions on the current template, then assert the
    migration's output is byte-identical to the current rendered template."""
    rendered = friday_identity._AGENTS_TEMPLATE.format(owner_display="Sam")
    legacy_body = rendered.replace(
        friday_identity._PAPERCLIP_SCRIPT_FENCE, friday_identity._LEGACY_PAPERCLIP_CURL_FENCE
    ).replace(friday_identity._PAPERCLIP_AUTH_SENTENCE, friday_identity._LEGACY_PAPERCLIP_AUTH_SENTENCE)
    assert legacy_body != rendered, "fixture must actually differ from the current template"

    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": legacy_body})
    assert friday_identity._migrate_paperclip_curl_to_script(ws, "owner_1", _UUID) is True
    assert ws.files[f"workspace-{_UUID}/AGENTS.md"] == rendered


def test_paperclip_curl_migration_is_idempotent():
    """A second run over an already-migrated body is a no-op."""
    body = (
        "intro prose before it.\n\n"
        + friday_identity._LEGACY_PAPERCLIP_CURL_FENCE
        + "\nmore prose. "
        + friday_identity._LEGACY_PAPERCLIP_AUTH_SENTENCE
        + " tail prose.\n"
    )
    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": body})

    assert friday_identity._migrate_paperclip_curl_to_script(ws, "owner_1", _UUID) is True
    once_healed = ws.files[f"workspace-{_UUID}/AGENTS.md"]
    assert friday_identity._migrate_paperclip_curl_to_script(ws, "owner_1", _UUID) is False
    assert ws.files[f"workspace-{_UUID}/AGENTS.md"] == once_healed


def test_paperclip_curl_migration_is_a_noop_without_the_legacy_anchor():
    """A Friday seeded with the current template (no legacy curl fence) is
    left untouched — this must never fire on already-current playbooks."""
    body = friday_identity._AGENTS_TEMPLATE.format(owner_display="Sam")
    ws = _DictWorkspace({f"workspace-{_UUID}/AGENTS.md": body})

    assert friday_identity._migrate_paperclip_curl_to_script(ws, "owner_1", _UUID) is False
    assert ws.files[f"workspace-{_UUID}/AGENTS.md"] == body


def test_legacy_grounding_section_swaps_once_and_ends_on_the_new_skill_id():
    """Ordering guard. The legacy section is matched BYTE-EXACTLY and contains the
    retired id, so the id repair must run after it. If it ran first the match would
    fail and the owner would end up with two grounding sections."""
    soul = "# SOUL\n\n" + friday_identity._LEGACY_SELF_GROUNDING_SECTION
    ws = _DictWorkspace({f"workspace-{_UUID}/SOUL.md": soul})

    friday_identity._run_heal_migrations(ws, "owner_1", _UUID)

    healed = ws.files[f"workspace-{_UUID}/SOUL.md"]
    assert healed.count(friday_identity._SELF_GROUNDING_HEADING) == 1
    assert friday_identity._LEGACY_SELF_GROUNDING_HEADING not in healed
    assert "skill isol8" not in healed


# ---------------------------------------------------------------------------
# Legacy "Lightbulb AI" content already on EFS
# ---------------------------------------------------------------------------

_LIGHTBULB_AI_HEADING = "## You are made of Lightbulb AI"


def test_legacy_lightbulb_ai_grounding_section_is_swapped_not_left_stale():
    """The trap this guards: the CURRENT heading is a strict prefix of the
    "Lightbulb AI" one, so checking it first reads an unmigrated file as
    already-current and the retired brand sticks to every owner forever."""
    for legacy_section in friday_identity._LEGACY_SELF_GROUNDING_SECTIONS[:2]:
        rel = f"workspace-{_UUID}/SOUL.md"
        ws = _DictWorkspace({rel: "# SOUL.md - Who You Are\n\nFriday's own evolved soul.\n" + legacy_section})

        assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is True
        migrated = ws.files[rel]

        assert migrated.count("## You are made of") == 1
        assert _LIGHTBULB_AI_HEADING not in migrated
        assert "Lightbulb AI" not in migrated
        assert "skill isol8" not in migrated
        assert "Friday's own evolved soul." in migrated

        # Idempotent: the new heading is now the matcher.
        assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False
        assert ws.files[rel] == migrated


def test_legacy_lightbulb_ai_grounding_heading_swapped_when_body_was_evolved():
    rel = f"workspace-{_UUID}/SOUL.md"
    ws = _DictWorkspace({rel: f"# SOUL\n\n{_LIGHTBULB_AI_HEADING}\n\nI rewrote this part in my own words.\n"})

    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is True
    migrated = ws.files[rel]

    assert migrated.count("## You are made of") == 1
    assert _LIGHTBULB_AI_HEADING not in migrated
    assert friday_identity._SELF_GROUNDING_HEADING in migrated
    assert "I rewrote this part in my own words." in migrated
    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False


def test_legacy_heading_replaced_on_its_own_line_not_at_an_earlier_inline_mention():
    """An inline mention of the retired heading BEFORE the real heading must not
    be what gets rewritten: the migration replaces the heading LINE."""
    rel = f"workspace-{_UUID}/SOUL.md"
    inline = f"Note to self: the seed once said {_LIGHTBULB_AI_HEADING} here."
    ws = _DictWorkspace(
        {rel: f"# SOUL\n\n{inline}\n\n{_LIGHTBULB_AI_HEADING}\n\nI rewrote this part in my own words.\n"}
    )

    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is True
    migrated = ws.files[rel]

    assert inline in migrated  # the owner's sentence is untouched
    assert f"\n{friday_identity._SELF_GROUNDING_HEADING}\n" in migrated  # the real heading moved
    assert f"\n{_LIGHTBULB_AI_HEADING}\n" not in migrated
    assert "I rewrote this part in my own words." in migrated
    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False


def test_stale_brand_fragments_repointed_in_user_md_and_agents_md():
    """USER.md's facts pointer and AGENTS.md's platform-mechanics line are
    written by the PLATFORM, and no other migration is anchored on them — a
    file seeded under the old brand keeps it with no path that heals it."""
    user_rel = f"workspace-{_UUID}/USER.md"
    agents_rel = f"workspace-{_UUID}/AGENTS.md"
    ws = _DictWorkspace(
        {
            user_rel: (
                "# USER.md - About You\n\n"
                "- **Email:** a@b.co\n"
                "- **Plan & provider:** read `~/.openclaw/isol8-facts.json` — Lightbulb AI keeps it\n"
                "  current; trust it over anything remembered.\n\n"
                "Owner prose mentioning Lightbulb AI in passing.\n"
            ),
            agents_rel: (
                "For platform mechanics — the Integrations tab, marketplace flows, billing\n"
                "surfaces, what agents can and cannot do on Lightbulb AI — run `skill lightbulb`.\n"
            ),
        }
    )

    assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is True

    assert friday_identity._FACTS_POINTER_LINE_1 in ws.files[user_rel]
    assert "Lightbulb AI keeps it" not in ws.files[user_rel]
    # Owner prose is NOT a platform fragment — it survives verbatim.
    assert "Owner prose mentioning Lightbulb AI in passing." in ws.files[user_rel]
    assert "cannot do on Lightbulb — run `skill lightbulb`." in ws.files[agents_rel]

    assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is False


def test_stale_brand_fragments_repoint_the_isol8_generation_too():
    rel = f"workspace-{_UUID}/USER.md"
    ws = _DictWorkspace(
        {rel: "- **Plan & provider:** read `~/.openclaw/isol8-facts.json` — Isol8 keeps it\n  current;\n"}
    )
    assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is True
    assert friday_identity._FACTS_POINTER_LINE_1 in ws.files[rel]


def test_stale_brand_fragments_repoint_the_pre_rename_facts_path():
    """Current brand wording, old facts path.

    The facts file moved isol8-facts.json -> lightbulb-facts.json AFTER the
    brand rename, so a USER.md seeded in between names Lightbulb but points at
    a path the backend now deletes. Without this fragment that file would tell
    Friday to read a file that no longer exists."""
    rel = f"workspace-{_UUID}/USER.md"
    ws = _DictWorkspace(
        {rel: "- **Plan & provider:** read `~/.openclaw/isol8-facts.json` — Lightbulb keeps it\n  current;\n"}
    )
    assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is True
    assert friday_identity._FACTS_POINTER_LINE_1 in ws.files[rel]
    assert "isol8-facts.json" not in ws.files[rel]


def test_heal_migrations_leave_a_lightbulb_ai_friday_fully_current():
    """End-to-end over the real migration list: one grounding section, one
    brand, and no retired skill id left anywhere."""
    soul_rel = f"workspace-{_UUID}/SOUL.md"
    user_rel = f"workspace-{_UUID}/USER.md"
    ws = _DictWorkspace(
        {
            soul_rel: "# SOUL\n\n" + friday_identity._LEGACY_SELF_GROUNDING_SECTIONS[1],
            user_rel: "- **Plan & provider:** read `~/.openclaw/isol8-facts.json` — Lightbulb AI keeps it\n",
        }
    )

    friday_identity._run_heal_migrations(ws, "owner_1", _UUID)

    healed_soul = ws.files[soul_rel]
    assert healed_soul.count(friday_identity._SELF_GROUNDING_HEADING) == 1
    assert "Lightbulb AI" not in healed_soul
    assert "skill isol8" not in healed_soul
    assert "Lightbulb AI" not in ws.files[user_rel]


def test_current_soul_md_quoting_a_retired_heading_is_left_alone():
    """The current heading is matched as a WHOLE LINE, so a Friday that quotes
    the rename in its own prose is not dragged back through the migration and
    left with two grounding headings."""
    rel = f"workspace-{_UUID}/SOUL.md"
    original = (
        "# SOUL\n"
        + friday_identity._SELF_GROUNDING_SECTION
        + "\nI once wrote: ## You are made of Lightbulb AI — then the name changed.\n"
    )
    ws = _DictWorkspace({rel: original})

    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False
    assert ws.files[rel] == original
    assert ws.files[rel].count(friday_identity._SELF_GROUNDING_HEADING) == 2  # heading + the quote


def test_current_heading_matches_without_a_trailing_newline():
    rel = f"workspace-{_UUID}/SOUL.md"
    ws = _DictWorkspace({rel: f"# SOUL\n\n{friday_identity._SELF_GROUNDING_HEADING}"})
    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False


def test_stale_brand_fragments_repoint_the_org_workspace_header():
    """Same class as the facts pointer: platform-authored, renamed by #2124,
    and nothing else is anchored on it."""
    rel = f"workspace-{_UUID}/USER.md"
    for legacy in ("Lightbulb AI", "Isol8"):
        ws = _DictWorkspace(
            {
                rel: (
                    "# USER.md - About This Workspace\n\n"
                    f"This is **Acme**'s shared workspace on {legacy}. The original owner is\n"
                    "ada@acme.co. As more members join the org, they share this container.\n"
                )
            }
        )
        assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is True
        migrated = ws.files[rel]
        assert "This is **Acme**'s shared workspace on Lightbulb. The original owner is" in migrated
        assert "ada@acme.co" in migrated
        assert friday_identity._migrate_stale_brand_fragments(ws, "owner_1", _UUID) is False


def test_inline_quote_of_a_retired_heading_is_not_a_grounding_section():
    """A SOUL.md with NO grounding section that merely mentions a retired
    heading mid-sentence must be appended to, not "rebranded in place" — the
    legacy check is whole-line for the same reason the current one is."""
    rel = f"workspace-{_UUID}/SOUL.md"
    ws = _DictWorkspace({rel: "# SOUL\n\nThe old docs said ## You are made of Lightbulb AI — that name is gone.\n"})

    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is True
    migrated = ws.files[rel]

    # The owner's sentence is untouched: the quote is still the retired wording.
    assert "The old docs said ## You are made of Lightbulb AI — that name is gone." in migrated
    # And a real section was APPENDED rather than the quote being rewritten.
    assert migrated.rstrip().endswith(friday_identity._SELF_GROUNDING_SECTION.rstrip())
    assert sum(line.strip() == friday_identity._SELF_GROUNDING_HEADING for line in migrated.splitlines()) == 1

    assert friday_identity._migrate_add_soul_grounding(ws, "owner_1", _UUID) is False


@pytest.mark.asyncio
async def test_paperclip_identities_refreshes_stale_api_url_keeping_token():
    """An EXISTING file whose apiUrl names a retired host is rewritten with the
    current URL, everything else (the agent's own, possibly rotated, token)
    untouched. The 2026-08-30 Cloud Map rename left every pre-rename container
    pointing at ``paperclip.isol8-{env}.local`` because this file was
    write-if-missing and nothing ever refreshed it."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=False)
    workspace.read_file = MagicMock(
        return_value=_json.dumps(
            {
                "token": "rotated-key",
                "agentId": "agent-friday",
                "companyId": "comp-1",
                "apiUrl": "http://paperclip.isol8-prod.local:3100",
            }
        )
    )

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id="comp-1",
        api_url="http://paperclip.lightbulb-prod.local:3100",
        workspace=workspace,
    )

    workspace.read_file.assert_called_once_with("user_abc", "workspace-agent-friday/paperclip-claimed-api-key.json")
    workspace.write_file.assert_called_once()
    owner, rel_path, content = workspace.write_file.call_args.args
    assert (owner, rel_path) == ("user_abc", "workspace-agent-friday/paperclip-claimed-api-key.json")
    assert _json.loads(content) == {
        "token": "rotated-key",
        "agentId": "agent-friday",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.lightbulb-prod.local:3100",
    }


@pytest.mark.asyncio
async def test_paperclip_identities_leaves_a_current_file_alone():
    """Same apiUrl on disk → no rewrite (reprovision stays a no-op on EFS)."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=False)
    workspace.read_file = MagicMock(
        return_value=_json.dumps(
            {"token": "k", "agentId": "agent-friday", "companyId": "comp-1", "apiUrl": "http://pc:3100"}
        )
    )

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_unreadable_existing_file_is_left_alone():
    """Best-effort: a file we cannot read or parse is neither rewritten nor
    fatal — a half-understood file must never be replaced with a guess."""
    from core.containers.workspace import WorkspaceError

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=False)
    workspace.read_file = MagicMock(side_effect=WorkspaceError("nope", user_id="user_abc"))

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file.assert_not_called()


@pytest.mark.asyncio
async def test_stage_agent_paperclip_identity_refreshes_stale_api_url():
    """The single-agent path (post-create pairing) refreshes a stale apiUrl too."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=False)
    workspace.read_file = MagicMock(
        return_value=_json.dumps(
            {
                "token": "own-key",
                "agentId": "agent-new",
                "companyId": "comp-1",
                "apiUrl": "http://paperclip.isol8-dev.local:3100",
            }
        )
    )

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-new",
        token="minted-key",
        company_id="comp-1",
        api_url="http://paperclip.lightbulb-dev.local:3100",
        workspace=workspace,
    )

    workspace.write_file.assert_called_once()
    content = workspace.write_file.call_args.args[2]
    assert _json.loads(content) == {
        "token": "own-key",
        "agentId": "agent-new",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.lightbulb-dev.local:3100",
    }


# --- Self-service: Friday must use the tools she has -------------------------
#
# Reported: asked to open a page in the browser, she ran marketplace_search and
# offered to hire someone. Three stacked causes, all in the seeded markdown:
# SOUL described the browser as desktop-gated (it is not), the orchestration
# loop had no terminal that was not delegation, and step 3 fired the marketplace
# check on a missing TOOL rather than a missing ROLE.
#
# Not a tool-configuration problem: marketplace_search is a plugin tool and is
# compacted by tool search exactly as `browser` is, so she reached one and not
# the other in the same turn. The prompt is the whole cause.


def test_soul_browser_line_states_the_container_browser_is_always_available():
    """config.py sets browser.defaultProfile="openclaw" -- the in-container
    Chromium -- on every container. The paired desktop is the OPTIONAL upgrade.
    Naming only the gated profile made a browsing request read as a capability
    Friday might not have."""
    line = friday_identity._SOUL_BROWSER_LINE
    assert "always" in line.lower()
    assert "when their desktop app is paired" not in line
    assert "signed-in" in line  # the paired-desktop upgrade is still described


def test_soul_browser_line_carries_no_format_placeholder():
    """str.format does not recurse into substituted values, so a placeholder
    inside this constant would reach disk literally. It must also stay
    byte-identical between the seed path and the heal migration, which has no
    owner display name to substitute."""
    assert "{" not in friday_identity._SOUL_BROWSER_LINE


def test_orchestration_loop_has_a_do_it_yourself_terminal():
    """Without a step-0 exit, a one-shot request runs decompose -> roster ->
    "no agent qualified" -> marketplace_search. The loop had no other terminal."""
    gate = friday_identity._AGENTS_DIY_GATE
    assert gate.lstrip().startswith("0.")
    for trigger in ("browser", "fetch", "read a file", "run a command", "answer"):
        assert trigger in gate.lower(), f"missing trigger case: {trigger}"
    # It must forbid the loop, not merely permit doing the work.
    assert "do not decompose" in gate.lower()
    assert "marketplace" in gate.lower()


def test_marketplace_check_is_scoped_to_a_missing_role():
    """Assert inside step 3's slice so line-wrapping cannot make this vacuous."""
    t = friday_identity._AGENTS_TEMPLATE
    step3 = t[t.index("3. **Identify or hire.") : t.index("4. **Confirm.")]
    assert "role nobody on the roster fills" in step3
    assert "is qualified" not in step3


def test_delegation_boundary_is_scoped_to_sustained_work():
    """A blanket "delegate it -- do not perform it yourself" bans doing the work
    at all, which is what sent a one-shot browsing request to the marketplace."""
    t = friday_identity._AGENTS_TEMPLATE
    assert "sustained" in t
    assert "delegate it — do not perform it yourself" not in t


def test_rendered_agents_md_has_real_margin_below_the_truncation_ceiling():
    """AGENTS.md is seeded whole into every turn's context.

    Measure the RENDERED file, not the template: {owner_display} appears 17
    times, so an org with a long display name produces a materially larger file
    than len(_AGENTS_TEMPLATE) suggests. A template-length assertion passes
    while a real container is over.

    Thresholds, from the fork at v2026.8.1-isol8.3:
      - DEFAULT_BOOTSTRAP_MAX_CHARS = 20_000 is the real cliff. Above it the
        file is TRUNCATED and a notice tells the agent her own playbook was cut.
      - nearLimit (85% = 17_000) is a DIAGNOSTIC FLAG in bootstrap-budget.ts,
        not a cliff -- `nearLimit` only annotates the budget analysis.

    We assert margin below the cliff rather than below the flag. Getting under
    17_000 for a long org name would mean deleting the key-leak, Authorization
    and run-id guards, and those are worth more than a diagnostic flag: a live
    Paperclip key reached an owner's transcript once already.
    """
    template = friday_identity._AGENTS_TEMPLATE
    occurrences = template.count("{owner_display}")
    assert occurrences > 0, "owner substitution disappeared; this test is now vacuous"

    # A long but realistic org display name.
    long_owner = "Acme Manufacturing Group"
    rendered = len(template) + occurrences * (len(long_owner) - len("{owner_display}"))

    assert rendered < 18_500, (
        f"rendered AGENTS.md is {rendered} chars for a {len(long_owner)}-char owner name; "
        "the truncation ceiling is 20,000 and truncation tells the agent her playbook was "
        "cut. Move reference material into the paperclip skill rather than raising this."
    )


# --- Self-service heal migration ---------------------------------------------
#
# SOUL.md and AGENTS.md are seeded write-if-missing, so the template fixes above
# reach NEW provisions only. Already-provisioned Fridays keep the copy that
# routes a browsing request to marketplace_search until this runs.


def _self_service_ws(store: dict):
    ws = MagicMock()
    ws.read_file = lambda owner_id, rel_path: store[rel_path]
    ws.write_file = lambda owner_id, rel_path, content: store.__setitem__(rel_path, content)
    return ws


def test_self_service_heal_replaces_the_legacy_browser_line():
    store = {
        "workspace-friday-uuid/SOUL.md": (
            "# SOUL.md\n\nintro\n" + friday_identity._LEGACY_SOUL_BROWSER_LINE + "\noutro\n"
        ),
        "workspace-friday-uuid/AGENTS.md": friday_identity._AGENTS_DIY_GATE,
    }
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    soul = store["workspace-friday-uuid/SOUL.md"]
    assert friday_identity._LEGACY_SOUL_BROWSER_LINE not in soul
    assert "always available" in soul
    assert "outro" in soul  # surrounding content preserved


def test_self_service_heal_is_idempotent():
    store = {
        "workspace-friday-uuid/SOUL.md": friday_identity._SOUL_BROWSER_LINE,
        "workspace-friday-uuid/AGENTS.md": friday_identity._AGENTS_DIY_GATE,
    }
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False


def test_self_service_heal_never_clobbers_an_edited_browser_line():
    """An owner- or Friday-edited line is theirs. Only the byte-exact legacy
    text moves; a heading match is never permission to overwrite."""
    edited = "- **Browser** — my own notes about the browser. Do not touch.\n"
    store = {
        "workspace-friday-uuid/SOUL.md": "# SOUL.md\n\n" + edited,
        "workspace-friday-uuid/AGENTS.md": friday_identity._AGENTS_DIY_GATE,
    }
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False
    assert store["workspace-friday-uuid/SOUL.md"] == "# SOUL.md\n\n" + edited


def test_self_service_heal_inserts_the_gate_into_an_existing_agents_md():
    store = {
        "workspace-friday-uuid/SOUL.md": friday_identity._SOUL_BROWSER_LINE,
        "workspace-friday-uuid/AGENTS.md": ("# AGENTS.md\n\n## The orchestration loop\n\n1. **Decompose.** ...\n"),
    }
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    agents = store["workspace-friday-uuid/AGENTS.md"]
    assert friday_identity._AGENTS_DIY_GATE.rstrip("\n") in agents
    # The gate goes directly under the heading, above step 1.
    assert agents.index("0. **Can you just do it?") < agents.index("1. **Decompose.")


def test_self_service_heal_skips_an_agents_md_with_no_loop_heading():
    """Evolved content without the anchor is left alone rather than guessed at."""
    store = {
        "workspace-friday-uuid/SOUL.md": friday_identity._SOUL_BROWSER_LINE,
        "workspace-friday-uuid/AGENTS.md": "# AGENTS.md\n\nOwner rewrote this entirely.\n",
    }
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False


def test_self_service_heal_survives_a_missing_file():
    """A Friday with no SOUL.md yet must not raise — the seed loop writes it."""
    ws = MagicMock()
    ws.read_file.side_effect = FileNotFoundError
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False


def test_self_service_heal_is_registered_in_the_rail():
    """A migration nobody calls heals nothing."""
    import inspect

    src = inspect.getsource(friday_identity._run_heal_migrations)
    assert "_migrate_self_service" in src


# --- Heal completeness: the migration must deliver the WHOLE playbook fix -----
#
# Verified on a real prod container 2026-09-08: the first version of
# _migrate_self_service healed the SOUL browser line and inserted the step-0
# gate, but left step 3 still saying "If no agent is qualified, check the
# marketplace first" and Boundaries still saying "delegate it — do not perform
# it yourself". Two of four, and the two it missed are the instructions that
# caused the bug. It also APPENDED without removing, so a healed AGENTS.md grew
# to 19,552 chars — 448 from the 20,000 truncation ceiling.
#
# These constants are the RENDERED form (single braces). The template carries
# `{{issueId}}` for str.format; the file on disk has `{issueId}`.


def test_legacy_constants_are_the_rendered_form_not_the_template_form():
    """A `{{` in a legacy constant means it was copied from the template and
    will never match the file on disk."""
    for name in (
        "_LEGACY_AGENTS_STEP3_HEAD",
        "_LEGACY_AGENTS_DELEGATE_BOUNDARY",
        "_LEGACY_AGENTS_PAPERCLIP_REFERENCE",
    ):
        val = getattr(friday_identity, name)
        assert "{{" not in val, f"{name} carries template-escaped braces"


def _healed_store(agents_body: str) -> dict:
    return {
        "workspace-friday-uuid/SOUL.md": friday_identity._SOUL_BROWSER_LINE,
        "workspace-friday-uuid/AGENTS.md": agents_body,
    }


def test_heal_rescopes_step_three_to_a_missing_role():
    store = _healed_store(
        "## The orchestration loop\n\n"
        + friday_identity._AGENTS_DIY_GATE
        + "\n"
        + friday_identity._LEGACY_AGENTS_STEP3_HEAD
    )
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    agents = store["workspace-friday-uuid/AGENTS.md"]
    assert "role nobody on the roster fills" in agents
    assert friday_identity._LEGACY_AGENTS_STEP3_HEAD not in agents


def test_heal_narrows_the_delegation_boundary():
    store = _healed_store(
        "## The orchestration loop\n\n"
        + friday_identity._AGENTS_DIY_GATE
        + "\n"
        + friday_identity._LEGACY_AGENTS_DELEGATE_BOUNDARY
    )
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    agents = store["workspace-friday-uuid/AGENTS.md"]
    assert "Delegate sustained specialist execution" in agents
    assert "delegate it — do not perform it yourself" not in agents


def test_heal_strips_the_paperclip_reference_so_the_file_shrinks():
    """The migration appended without removing, which grew the file on exactly
    the containers that already had the problem."""
    body = (
        "## The orchestration loop\n\n"
        + friday_identity._AGENTS_DIY_GATE
        + "\n"
        + friday_identity._LEGACY_AGENTS_PAPERCLIP_REFERENCE
    )
    store = _healed_store(body)
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    healed = store["workspace-friday-uuid/AGENTS.md"]
    assert len(healed) < len(body), "heal must shrink, not grow, an oversized playbook"
    assert "GET /api/agents/me" not in healed
    assert "skill paperclip" in healed  # the pointer replaces the tables


def test_heal_never_clobbers_an_edited_step_three():
    """Byte-exact or nothing — an owner-edited step 3 is theirs."""
    edited = "3. **Identify or hire.** My own rewritten step three.\n"
    body = "## The orchestration loop\n\n" + friday_identity._AGENTS_DIY_GATE + "\n" + edited
    store = _healed_store(body)
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False
    assert store["workspace-friday-uuid/AGENTS.md"] == body


def test_heal_is_idempotent_across_all_four_changes():
    body = (
        "## The orchestration loop\n\n"
        + friday_identity._AGENTS_DIY_GATE
        + "\n"
        + friday_identity._LEGACY_AGENTS_STEP3_HEAD
        + friday_identity._LEGACY_AGENTS_DELEGATE_BOUNDARY
        + friday_identity._LEGACY_AGENTS_PAPERCLIP_REFERENCE
    )
    store = _healed_store(body)
    ws = _self_service_ws(store)
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is True
    once = store["workspace-friday-uuid/AGENTS.md"]
    assert friday_identity._migrate_self_service(ws, "user_abc", "friday-uuid") is False
    assert store["workspace-friday-uuid/AGENTS.md"] == once
