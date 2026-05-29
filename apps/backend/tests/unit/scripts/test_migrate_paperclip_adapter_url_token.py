"""Unit tests for the adapter-URL-token backfill script."""

import os
from urllib.parse import parse_qs, urlsplit

import jwt

# service_token.mint() reads PAPERCLIP_SERVICE_TOKEN_KEY at call time;
# pin a deterministic key so the mint-on-missing path can be decoded.
os.environ.setdefault(
    "PAPERCLIP_SERVICE_TOKEN_KEY",
    "test-key-migrate-paperclip-adapter-url-token",
)

from scripts.migrate_paperclip_adapter_url_token import (  # noqa: E402
    migrate_one_agent,
    needs_migration,
)


def test_needs_migration_true_when_url_has_no_token():
    assert needs_migration(
        {"adapterConfig": {"url": "wss://ws.isol8.co/", "authToken": "tok"}},
    )


def test_needs_migration_false_when_url_already_has_token():
    assert not needs_migration(
        {"adapterConfig": {"url": "wss://ws.isol8.co/?token=abc", "authToken": "tok"}},
    )


def test_needs_migration_false_when_url_missing():
    """Non-openclaw_gateway adapters (no url) skip."""
    assert not needs_migration({"adapterConfig": {}})
    assert not needs_migration({})


def test_migrate_one_agent_appends_existing_auth_token():
    agent = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "adapterConfig": {
            "url": "wss://ws.isol8.co/",
            "authToken": "existing-token",
            "sessionKeyStrategy": "issue",
        },
    }

    new_cfg = migrate_one_agent(agent, owner_id="owner_x")

    assert new_cfg["url"] == "wss://ws.isol8.co/?token=existing-token"
    # authToken reused, not re-minted.
    assert new_cfg["authToken"] == "existing-token"
    # Other fields preserved verbatim.
    assert new_cfg["sessionKeyStrategy"] == "issue"


def test_migrate_one_agent_strips_pre_existing_query():
    """Any pre-existing query string is dropped before appending token."""
    agent = {
        "id": "id",
        "adapterConfig": {
            "url": "wss://ws.isol8.co/?foo=bar",
            "authToken": "tok",
        },
    }

    new_cfg = migrate_one_agent(agent, owner_id="owner_x")

    assert new_cfg["url"] == "wss://ws.isol8.co/?token=tok"
    assert parse_qs(urlsplit(new_cfg["url"]).query) == {"token": ["tok"]}


def test_migrate_one_agent_mints_token_when_missing():
    """No authToken on the agent → mint a fresh owner-scoped token and
    embed it (and persist it back onto adapterConfig)."""
    agent = {
        "id": "id",
        "adapterConfig": {"url": "wss://ws.isol8.co/"},
    }

    new_cfg = migrate_one_agent(agent, owner_id="org_xyz")

    minted = new_cfg["authToken"]
    assert minted
    assert new_cfg["url"] == f"wss://ws.isol8.co/?token={minted}"
    claims = jwt.decode(
        minted,
        os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"],
        algorithms=["HS256"],
    )
    assert claims["sub"] == "org_xyz"
