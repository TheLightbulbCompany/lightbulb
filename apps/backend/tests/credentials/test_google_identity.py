"""Tests for per-agent Google-account binding (google_identity).

``seed_agent_google_identity`` writes the agent's explicit assignment into its
Hermes profile as ``GOG_ACCOUNT`` — gog's own default-account variable, which
the bridge delivers to the turn's shell. There is no default fallback: an
unassigned agent gets the key deleted and falls back to bare ``gog``.

Seeding is user-action-driven — only the assign API and the disconnect path
call it. The repo resolution and the profile writer are mocked, so no S3 or
DynamoDB is touched.
"""

from unittest.mock import AsyncMock

import pytest

from core.services import google_identity
from core.services.google_identity import seed_agent_google_identity


@pytest.fixture
def cli_env(monkeypatch):
    """A stand-in for the profile cli-env writer."""
    setter = AsyncMock()
    monkeypatch.setattr("core.services.credentials.hermes_cli.set_profile_cli_env", setter, raising=False)
    return setter


@pytest.fixture
def patch_resolver(monkeypatch):
    """Patch get_assignment to return a controllable email.

    The seeder reflects ONLY an agent's explicit assignment (no default
    fallback): an email binds the account, None clears it.
    """

    def _set(email):
        monkeypatch.setattr(
            google_identity.google_accounts_repo,
            "get_assignment",
            AsyncMock(return_value=email),
        )

    return _set


@pytest.mark.asyncio
async def test_assignment_is_delivered_as_gog_account_env(cli_env, patch_resolver):
    patch_resolver("alice@gmail.com")

    await seed_agent_google_identity("org_1", "agent_x")

    cli_env.assert_awaited_once_with("org_1", "agent_x", {"GOG_ACCOUNT": "alice@gmail.com"})


@pytest.mark.asyncio
async def test_unassigned_deletes_the_key(cli_env, patch_resolver):
    patch_resolver(None)

    await seed_agent_google_identity("org_1", "agent_x")

    cli_env.assert_awaited_once_with("org_1", "agent_x", {"GOG_ACCOUNT": None})


@pytest.mark.asyncio
async def test_reassignment_writes_the_new_account(cli_env, patch_resolver):
    """Reassignment is a plain overwrite — the profile holds one value per key,
    so there is no stale binding to strip the way the old managed block had."""
    patch_resolver("alice@gmail.com")
    await seed_agent_google_identity("org_1", "agent_x")
    patch_resolver("bob@gmail.com")
    await seed_agent_google_identity("org_1", "agent_x")

    assert [call.args for call in cli_env.await_args_list] == [
        ("org_1", "agent_x", {"GOG_ACCOUNT": "alice@gmail.com"}),
        ("org_1", "agent_x", {"GOG_ACCOUNT": "bob@gmail.com"}),
    ]
