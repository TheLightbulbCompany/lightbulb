"""Unit tests for the session-strategy migration script."""

import os
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

# service_token.mint() reads PAPERCLIP_SERVICE_TOKEN_KEY at call time;
# pin a deterministic key so the rotation tests can decode the issued
# JWT and assert on its ``sub`` claim.
os.environ.setdefault(
    "PAPERCLIP_SERVICE_TOKEN_KEY",
    "test-key-migrate-paperclip-session-strategy",
)

from scripts.migrate_paperclip_session_strategy import (  # noqa: E402
    _token_sub,
    migrate_one_agent,
    needs_migration,
)


def _mint_test_token(sub: str) -> str:
    """Mint a JWT with the test signing key. Mirrors the real
    ``service_token.mint`` claim shape for the bits this script reads.
    """
    return jwt.encode(
        {"sub": sub, "kind": "paperclip_service"},
        os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"],
        algorithm="HS256",
    )


def test_token_sub_returns_sub_claim():
    token = _mint_test_token("owner_abc")
    assert _token_sub(token) == "owner_abc"


def test_token_sub_returns_none_for_malformed_token():
    assert _token_sub("not-a-jwt") is None
    assert _token_sub("") is None
    assert _token_sub(None) is None


def test_needs_migration_true_for_fixed_strategy():
    """Fixed strategy is the legacy trigger — flip to ``issue``."""
    assert needs_migration(
        {"adapterConfig": {"sessionKeyStrategy": "fixed", "sessionKey": "user_x"}},
        owner_id="owner_x",
    )


def test_needs_migration_false_for_issue_strategy_and_matching_token():
    """Already-migrated agent with a correctly-keyed token is a no-op."""
    token = _mint_test_token("org_x")
    assert not needs_migration(
        {
            "adapterConfig": {
                "sessionKeyStrategy": "issue",
                "sessionKey": "user_member",
                "authToken": token,
            },
        },
        owner_id="org_x",
    )


def test_needs_migration_true_when_token_sub_mismatches_owner():
    """P1 finding 4: legacy org rows where authToken sub == member's
    user_id (instead of org_id) MUST be migrated, even if their
    sessionKeyStrategy is already ``"issue"``.
    """
    member_keyed_token = _mint_test_token("user_member_b")
    assert needs_migration(
        {
            "adapterConfig": {
                "sessionKeyStrategy": "issue",
                "sessionKey": "user_member_b",
                "authToken": member_keyed_token,
            },
        },
        owner_id="org_xyz",
    )


def test_needs_migration_false_when_owner_id_unknown():
    """If we can't resolve the company's canonical owner_id (no row
    found, or the row predates the org_id column), we don't speculate
    on whether the token is wrong — only the strategy flip remains
    eligible.
    """
    member_keyed_token = _mint_test_token("user_x")
    assert not needs_migration(
        {
            "adapterConfig": {
                "sessionKeyStrategy": "issue",
                "authToken": member_keyed_token,
            },
        },
        owner_id=None,
    )


def test_needs_migration_false_when_adapter_config_missing():
    """Non-openclaw_gateway agents (e.g., upstream openclaw-cli) skip."""
    assert not needs_migration({"adapterConfig": {}}, owner_id="org_x")
    assert not needs_migration({}, owner_id="org_x")


@pytest.mark.asyncio
async def test_migrate_one_agent_flips_strategy():
    """Strategy flip alone — token already keyed at owner_id, so
    rotation is a no-op."""
    admin_client = MagicMock()
    admin_client.patch_agent = AsyncMock()
    admin_cookie = "cookie"

    correct_token = _mint_test_token("user_personal")
    agent = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "companyId": "co_xyz",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": correct_token,
            "sessionKeyStrategy": "fixed",
            "sessionKey": "user_personal",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie=admin_cookie,
        agent=agent,
        owner_id="user_personal",
    )

    admin_client.patch_agent.assert_awaited_once()
    kwargs = admin_client.patch_agent.await_args.kwargs
    assert kwargs["agent_id"] == agent["id"]
    new_cfg = kwargs["body"]["adapterConfig"]
    assert new_cfg["sessionKeyStrategy"] == "issue"
    # Token unchanged because sub already matched owner_id.
    assert new_cfg["authToken"] == correct_token
    # Other fields preserved verbatim.
    assert new_cfg["sessionKey"] == "user_personal"
    assert new_cfg["url"] == "wss://ws.isol8.co"
    assert kwargs["body"]["replaceAdapterConfig"] is True


@pytest.mark.asyncio
async def test_migrate_one_agent_rotates_org_token_to_owner_sub():
    """P1 finding 4: a legacy org row whose authToken was minted with
    the member's user_id MUST be re-minted with sub=owner_id (org_id).
    """
    admin_client = MagicMock()
    admin_client.patch_agent = AsyncMock()
    admin_cookie = "cookie"

    member_keyed_token = _mint_test_token("user_member_b")
    agent = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "companyId": "co_xyz",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": member_keyed_token,
            "sessionKeyStrategy": "issue",
            "sessionKey": "user_member_b",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie=admin_cookie,
        agent=agent,
        owner_id="org_xyz",
    )

    admin_client.patch_agent.assert_awaited_once()
    kwargs = admin_client.patch_agent.await_args.kwargs
    new_cfg = kwargs["body"]["adapterConfig"]
    # Strategy already ``"issue"`` — preserved.
    assert new_cfg["sessionKeyStrategy"] == "issue"
    # authToken was rotated.
    assert new_cfg["authToken"] != member_keyed_token
    rotated_claims = jwt.decode(
        new_cfg["authToken"],
        os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"],
        algorithms=["HS256"],
    )
    assert rotated_claims["sub"] == "org_xyz"
    assert rotated_claims["kind"] == "paperclip_service"
    # Adapter sessionKey preserved (per-creator manual-wake fallback).
    assert new_cfg["sessionKey"] == "user_member_b"
    assert new_cfg["url"] == "wss://ws.isol8.co"


@pytest.mark.asyncio
async def test_migrate_one_agent_personal_token_unchanged():
    """Regression guard for personal owners: when authToken sub already
    equals owner_id (== user_id for personal accounts), the migration
    must NOT rotate it.
    """
    admin_client = MagicMock()
    admin_client.patch_agent = AsyncMock()

    personal_token = _mint_test_token("user_personal")
    agent = {
        "id": "11111111-1111-1111-1111-111111111111",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": personal_token,
            "sessionKeyStrategy": "fixed",
            "sessionKey": "user_personal",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie="cookie",
        agent=agent,
        owner_id="user_personal",
    )

    new_cfg = admin_client.patch_agent.await_args.kwargs["body"]["adapterConfig"]
    assert new_cfg["authToken"] == personal_token


@pytest.mark.asyncio
async def test_migrate_one_agent_skips_rotation_when_owner_unknown():
    """Defensive: if we couldn't resolve the company's owner_id, leave
    authToken alone (don't speculate). Only the strategy flip applies.
    """
    admin_client = MagicMock()
    admin_client.patch_agent = AsyncMock()

    legacy_token = _mint_test_token("some_legacy_sub")
    agent = {
        "id": "22222222-2222-2222-2222-222222222222",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": legacy_token,
            "sessionKeyStrategy": "fixed",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie="cookie",
        agent=agent,
        owner_id=None,
    )

    new_cfg = admin_client.patch_agent.await_args.kwargs["body"]["adapterConfig"]
    assert new_cfg["sessionKeyStrategy"] == "issue"
    # Token preserved verbatim — owner unknown, no rotation.
    assert new_cfg["authToken"] == legacy_token
