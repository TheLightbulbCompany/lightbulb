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
    workspace = MagicMock()
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

    # Seven files written under the right path: 6 markdown templates + the
    # .openclaw/workspace-state.json marker that prevents OpenClaw's boot-time
    # reconcile from deleting BOOTSTRAP.md.
    assert workspace.write_file_if_missing.call_count == 7
    paths = [call.args[1] for call in workspace.write_file_if_missing.call_args_list]
    assert all(p.startswith("workspace-f47ac10b-58cc-4372-a567-0e02b2c3d479/") for p in paths)
    assert sorted(p.rsplit("/", 1)[-1] for p in paths) == [
        "AGENTS.md",
        "BOOTSTRAP.md",
        "IDENTITY.md",
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
        "workspace-state.json",
    ]

    # Content interpolation works.
    contents_by_name = {
        call.args[1].rsplit("/", 1)[-1]: call.args[2] for call in workspace.write_file_if_missing.call_args_list
    }
    assert "Friday" in contents_by_name["IDENTITY.md"]
    assert "Sam's AI company" in contents_by_name["SOUL.md"]
    assert "sam@example.com" in contents_by_name["USER.md"]
    assert "Sam" in contents_by_name["BOOTSTRAP.md"]

    # MEMORY.md is seeded as a structured durable-memory scaffold so every
    # Friday starts with a place to record decisions / preferences / facts.
    memory_md = contents_by_name["MEMORY.md"]
    assert "# MEMORY.md" in memory_md
    assert "## About Sam" in memory_md
    assert "## Operating Decisions" in memory_md
    assert "## Known Issues & Workarounds" in memory_md
    assert "## Lessons Learned" in memory_md

    # BOOTSTRAP.md must explicitly forbid tool use on the opening turn — the
    # earlier template said "Delete this file" and frontier models would
    # interpret that as an actionable task ahead of greeting the user.
    bootstrap_md = contents_by_name["BOOTSTRAP.md"].lower()
    assert "text only" in bootstrap_md
    assert "do not run any tools" in bootstrap_md
    assert "do not try to delete this file" in bootstrap_md

    # BOOTSTRAP greeting must plant the orchestration seed when the user
    # describes a goal — telegraphs Friday's concierge capability so users
    # know to hand her multi-step work.
    assert "orchestrate" in bootstrap_md or "specialists" in bootstrap_md

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
    # Per-agent identity: load from the workspace claimed-key file before any
    # Paperclip call.
    assert "paperclip-claimed-api-key.json" in agents_md
    assert "PAPERCLIP_API_KEY=$(jq -r .token" in agents_md
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
    workspace = MagicMock()
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
    """Reprovision case: write_file_if_missing returns False → no error,
    metric recorded for skipped_existing."""
    workspace = MagicMock()
    # All writes return False (files already exist on EFS).
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

    # Still attempted all seven writes (6 markdown + 1 state file);
    # write_file_if_missing handles the skip.
    assert workspace.write_file_if_missing.call_count == 7


@pytest.mark.asyncio
async def test_state_file_written_first_with_setup_completed(monkeypatch):
    """workspace-state.json:
    - is the FIRST write (so a partial template-write failure still leaves
      OpenClaw in setup-completed state and BOOTSTRAP.md isn't deleted)
    - lives under .openclaw/ at the right path
    - contains setupCompletedAt (the field upstream's reconcile checks)
    """
    import json as _json

    workspace = MagicMock()
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
    assert first_path == "workspace-uuid-1/.openclaw/workspace-state.json"

    state = _json.loads(first_content)
    assert state["version"] == 1
    # setupCompletedAt is the field reconcileWorkspaceBootstrapCompletionState
    # checks at upstream src/agents/workspace.ts:278-282 — must be non-empty.
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

    workspace = MagicMock()
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
    workspace = MagicMock()

    # First call (state file) raises; subsequent calls (6 templates) succeed.
    side_effects = [OSError("disk full")] + [True] * 6
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

    # 1 state-file attempt (failed) + 6 template writes = 7 calls.
    assert workspace.write_file_if_missing.call_count == 7
    template_names = sorted(c.args[1].rsplit("/", 1)[-1] for c in workspace.write_file_if_missing.call_args_list[1:])
    assert template_names == ["AGENTS.md", "BOOTSTRAP.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "USER.md"]


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


def test_migrate_legacy_user_md_swaps_lines_and_preserves_content():
    ws = _MigrationWs(_LEGACY_USER_MD)
    assert friday_identity._migrate_legacy_user_md(ws, "user_abc", "uuid-f") is True
    path, content = ws.written
    assert path == "workspace-uuid-f/USER.md"
    assert "isol8-facts.json" in content
    assert "Isol8 plan" not in content
    assert "- **Provider:**" not in content
    # Friday's evolved content survives untouched.
    assert "health-tech startup" in content
    assert "- **Name:** Sam Rivers" in content


def test_migrate_legacy_user_md_noop_on_new_template():
    ws = _MigrationWs("# USER.md\n- **Plan & provider:** read `~/.openclaw/isol8-facts.json`\n")
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
    assert "isol8-facts.json" in content


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
    assert "isol8-facts.json" in migrated
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


def test_agents_md_contains_nightly_section_and_dual_condition():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert "## Nightly self-directed wake" in agents_md
    # BOTH server-verified conditions must be present (originKind + marker).
    assert "routine_execution" in agents_md
    assert "[NIGHTLY-CEO-LOOP]" in agents_md
    # The one-task cost bound, stated in the loop.
    assert "ONE task per" in agents_md
    assert "do not invent new work" in agents_md.lower() or "never invent work" in agents_md.lower()


def test_agents_md_wake_reason_list_includes_issue_assigned():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    assert "issue_assigned" in agents_md


def test_agents_md_nightly_overrides_never_look_for_unassigned_work():
    agents_md = friday_identity._render_templates(_personal_profile())["AGENTS.md"]
    # The section explicitly frames itself as the exception to the no-unassigned
    # rule that stands on every other wake.
    assert "never look for unassigned work" in agents_md
    assert "This is the ONE wake where you may survey unassigned work" in agents_md


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
