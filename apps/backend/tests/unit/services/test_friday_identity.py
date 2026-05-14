"""Unit tests for friday_identity.pre_stage_friday_identity."""

from unittest.mock import MagicMock

import pytest

from core.services import friday_identity


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
        "provider_label": "Bedrock (Claude, Isol8-managed credits)",
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
    # .openclaw/workspace-state.json marker that prevents OpenClaw's boot-time
    # reconcile from deleting BOOTSTRAP.md.
    assert workspace.write_file_if_missing.call_count == 6
    paths = [call.args[1] for call in workspace.write_file_if_missing.call_args_list]
    assert all(p.startswith("workspaces/f47ac10b-58cc-4372-a567-0e02b2c3d479/") for p in paths)
    assert sorted(p.rsplit("/", 1)[-1] for p in paths) == [
        "AGENTS.md",
        "BOOTSTRAP.md",
        "IDENTITY.md",
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

    # AGENTS.md is the orchestration playbook — decompose, delegate, triage.
    # Must reference the native Paperclip skill + REST API so Friday calls
    # the canonical endpoints rather than custom backend wrappers. Per
    # dev feedback on PR #640: orchestration lives in Paperclip, not in
    # our backend.
    agents_md = contents_by_name["AGENTS.md"]
    # Native paperclip skill + API auth surface.
    assert "paperclip" in agents_md.lower()
    assert "PAPERCLIP_API_KEY" in agents_md
    assert "PAPERCLIP_RUN_ID" in agents_md
    # Canonical endpoints for the orchestration loop.
    assert "POST /api/companies" in agents_md and "/issues" in agents_md
    assert "/heartbeat-context" in agents_md
    # Wake-context env vars Friday must read on heartbeat triage.
    assert "PAPERCLIP_WAKE_REASON" in agents_md
    assert "PAPERCLIP_TASK_ID" in agents_md
    # In-house roster present so Friday has baseline knowledge of
    # specialists before her first roster fetch.
    assert "Atlas" in agents_md
    assert "Pulse" in agents_md
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
        "provider_label": "Bedrock (Claude, Isol8-managed credits)",
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
    # All four writes return False (files already exist on EFS).
    workspace.write_file_if_missing = MagicMock(return_value=False)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
            "provider_label": "Bedrock (Claude, Isol8-managed credits)",
        }

    monkeypatch.setattr(friday_identity, "_resolve_owner_profile", fake_resolve)

    # Should not raise.
    await friday_identity.pre_stage_friday_identity(
        owner_id="user_abc",
        paperclip_agents=[{"id": "uuid-1", "role": "ceo"}],
        workspace=workspace,
    )

    # Still attempted all six writes (5 markdown + 1 state file);
    # write_file_if_missing handles the skip.
    assert workspace.write_file_if_missing.call_count == 6


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
            "provider_label": "Bedrock (Claude, Isol8-managed credits)",
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
    assert first_path == "workspaces/uuid-1/.openclaw/workspace-state.json"

    state = _json.loads(first_content)
    assert state["version"] == 1
    # setupCompletedAt is the field reconcileWorkspaceBootstrapCompletionState
    # checks at upstream src/agents/workspace.ts:278-282 — must be non-empty.
    assert isinstance(state["setupCompletedAt"], str)
    assert state["setupCompletedAt"]
    assert isinstance(state["bootstrapSeededAt"], str)
    assert state["bootstrapSeededAt"]


@pytest.mark.asyncio
async def test_state_file_failure_does_not_block_template_writes(monkeypatch):
    """If workspace-state.json write raises, the four markdown templates
    still write (best-effort: state file is the floor, not the ceiling)."""
    workspace = MagicMock()

    # First call (state file) raises; subsequent calls (4 templates) succeed.
    side_effects = [OSError("disk full")] + [True] * 4
    workspace.write_file_if_missing = MagicMock(side_effect=side_effects)

    async def fake_resolve(owner_id):
        return {
            "kind": "personal",
            "owner_display": "Sam",
            "first_name_or_fallback": "Sam",
            "full_name": "Sam Rivers",
            "email": "sam@example.com",
            "provider_label": "Bedrock (Claude, Isol8-managed credits)",
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
    assert template_names == ["AGENTS.md", "BOOTSTRAP.md", "IDENTITY.md", "SOUL.md", "USER.md"]


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_substring",
    [
        ("bedrock_claude", None, "Bedrock"),
        ("chatgpt_oauth", None, "ChatGPT"),
        ("byo_key", "anthropic", "BYO Anthropic"),
        ("byo_key", "openai", "BYO OpenAI"),
    ],
)
def test_provider_label_for_each_choice(provider_choice, byo_provider, expected_substring):
    """_format_provider_label maps each provider tuple to a human label."""
    label = friday_identity._format_provider_label(provider_choice, byo_provider)
    assert expected_substring in label


@pytest.mark.asyncio
async def test_resolve_personal_owner_uses_clerk_get_user(monkeypatch):
    """Personal owner_id → reads first_name/last_name/email from clerk_admin."""
    from core.services import clerk_admin
    from core.services import container_provision

    async def fake_get_user(user_id):
        assert user_id == "user_abc"
        return {
            "first_name": "Sam",
            "last_name": "Rivers",
            "email_addresses": [{"email_address": "sam@example.com"}],
            "primary_email_address_id": None,
        }

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["kind"] == "personal"
    assert profile["full_name"] == "Sam Rivers"
    assert profile["first_name_or_fallback"] == "Sam"
    assert profile["owner_display"] == "Sam"
    assert profile["email"] == "sam@example.com"
    assert "Bedrock" in profile["provider_label"]


@pytest.mark.asyncio
async def test_resolve_org_owner_uses_clerk_get_organization(monkeypatch):
    """Org owner_id → reads org name from clerk_admin.get_organization
    and owner email from paperclip_owner_email."""
    from core.services import clerk_admin
    from core.services import container_provision
    from core.services import paperclip_owner_email

    async def fake_get_org(org_id):
        assert org_id == "org_xyz"
        return {"name": "Acme Inc", "created_by": "user_founder_123"}

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        assert org_id == "org_xyz"
        assert fallback_user_id == "user_founder_123"
        return "founder@acme.com"

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

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
    from core.services import container_provision

    async def fake_get_user_returns_none(user_id):
        return None

    async def fake_resolve_provider(owner_id):
        return ("byo_key", "anthropic")

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user_returns_none)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["kind"] == "personal"
    assert profile["email"] == "(unknown)"
    assert profile["full_name"] == "(unknown)"
    # Provider still resolves correctly via billing_repo.
    assert "BYO Anthropic" in profile["provider_label"]


@pytest.mark.asyncio
async def test_resolve_personal_owner_falls_back_to_email_local_part(monkeypatch):
    """No first_name in Clerk → first_name_or_fallback derives from email local-part."""
    from core.services import clerk_admin
    from core.services import container_provision

    async def fake_get_user(user_id):
        return {
            "first_name": None,
            "last_name": None,
            "email_addresses": [{"id": "ea_1", "email_address": "tao@example.com"}],
            "primary_email_address_id": "ea_1",
        }

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_user", fake_get_user)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    profile = await friday_identity._resolve_owner_profile("user_abc")
    assert profile["first_name_or_fallback"] == "tao"
    assert profile["owner_display"] == "tao"
    assert profile["full_name"] == "(unknown)"
    assert profile["email"] == "tao@example.com"


@pytest.mark.asyncio
async def test_resolve_org_owner_falls_back_when_clerk_returns_no_org(monkeypatch):
    """clerk_admin.get_organization returns None → org_name falls back to 'your team'."""
    from core.services import clerk_admin
    from core.services import container_provision
    from core.services import paperclip_owner_email

    async def fake_get_org(org_id):
        return None

    async def fake_lookup_owner_email(*, org_id, fallback_user_id):
        # fallback_user_id should be None when org has no created_by.
        assert fallback_user_id is None
        return None

    async def fake_resolve_provider(owner_id):
        return ("bedrock_claude", None)

    monkeypatch.setattr(clerk_admin, "get_organization", fake_get_org)
    monkeypatch.setattr(paperclip_owner_email, "lookup_owner_email", fake_lookup_owner_email)
    monkeypatch.setattr(container_provision, "resolve_provider_choice", fake_resolve_provider)

    profile = await friday_identity._resolve_owner_profile("org_xyz")
    assert profile["kind"] == "org"
    assert profile["org_name"] == "your team"
    assert profile["owner_email"] == "(unknown)"
    assert profile["owner_display"] == "your team"
