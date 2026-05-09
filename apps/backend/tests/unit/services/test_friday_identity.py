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

    # Four files written under the right path.
    assert workspace.write_file_if_missing.call_count == 4
    paths = [call.args[1] for call in workspace.write_file_if_missing.call_args_list]
    assert all(p.startswith("workspaces/f47ac10b-58cc-4372-a567-0e02b2c3d479/") for p in paths)
    assert sorted(p.rsplit("/", 1)[-1] for p in paths) == [
        "BOOTSTRAP.md",
        "IDENTITY.md",
        "SOUL.md",
        "USER.md",
    ]

    # Content interpolation works.
    contents_by_name = {
        call.args[1].rsplit("/", 1)[-1]: call.args[2] for call in workspace.write_file_if_missing.call_args_list
    }
    assert "Friday" in contents_by_name["IDENTITY.md"]
    assert "Sam's AI company" in contents_by_name["SOUL.md"]
    assert "sam@example.com" in contents_by_name["USER.md"]
    assert "Sam" in contents_by_name["BOOTSTRAP.md"]


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

    # Still attempted all four writes (write_file_if_missing handles the skip).
    assert workspace.write_file_if_missing.call_count == 4


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
