"""Rotated-seed harvest — copying the container's live ChatGPT tokens back to DDB.

The DDB oauth row is a connect-time seed; the container rotates the refresh
token in its own SQLite auth store, whose only backend-reachable copy is the
Litestream S3 replica. The harvest (run right before a wipe destroys that
replica) restores the DB, extracts the rotated ``openai:default`` profile and
writes it back over the spent seed — so a reprovision stages a credential
OpenAI still accepts.
"""

import json
import sqlite3

import boto3
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.encryption import get_fernet
from core.services import oauth_service
from core.services.oauth_service import (
    _live_profile_is_fresher,
    _read_live_profile_from_db,
    harvest_rotated_oauth_seed,
)


@pytest.fixture
def oauth_table(monkeypatch):
    from core.config import settings as _settings

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-oauth-tokens",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        # core.encryption.get_fernet reads settings.ENCRYPTION_KEY (cached at
        # import time) — patching the env var alone is insufficient.
        monkeypatch.setattr(_settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield


def _write_store_db(path, store: dict | str | None) -> str:
    """Build an agent DB with the auth_profile_store schema the fleet writes."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE auth_profile_store (store_key TEXT PRIMARY KEY, store_json TEXT)")
    if store is not None:
        payload = store if isinstance(store, str) else json.dumps(store)
        con.execute("INSERT INTO auth_profile_store VALUES ('primary', ?)", (payload,))
    con.commit()
    con.close()
    return str(path)


def _profile(refresh="rt-rotated", access="at-rotated", expires=2_000_000, **extra) -> dict:
    return {"type": "oauth", "provider": "openai", "access": access, "refresh": refresh, "expires": expires, **extra}


def _seed_active_row(user_id: str, *, refresh="rt-seed", expires_at=1_000_000, bound_owner_id=None) -> dict:
    tokens = {
        "access_token": "at-seed",
        "refresh_token": refresh,
        "id_token": "idt-seed",
        "account_id": "acc-1",
        "expires_at": expires_at,
    }
    item = {
        "user_id": user_id,
        "state": "active",
        "encrypted_tokens": get_fernet().encrypt(json.dumps(tokens).encode()),
        "account_id": "acc-1",
        "bound_owner_id": bound_owner_id or user_id,
    }
    boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens").put_item(Item=item)
    return tokens


def _read_row_tokens(user_id: str) -> dict:
    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table("test-oauth-tokens")
        .get_item(Key={"user_id": user_id})["Item"]
    )
    return json.loads(get_fernet().decrypt(bytes(row["encrypted_tokens"])).decode())


# --- _read_live_profile_from_db ------------------------------------------------


def test_reads_rotated_openai_profile(tmp_path):
    db = _write_store_db(tmp_path / "a.sqlite", {"version": 1, "profiles": {"openai:default": _profile()}})
    profile = _read_live_profile_from_db(db)
    assert profile["refresh"] == "rt-rotated"
    assert profile["access"] == "at-rotated"


@pytest.mark.parametrize(
    "store",
    [
        None,  # table exists, no primary row
        "not-json",
        {"version": 1, "profiles": {}},  # no openai profile
        {"version": 1, "profiles": {"openai:default": {"type": "api_key", "key": "k"}}},  # wrong type
        {"version": 1, "profiles": {"openai:default": {"type": "oauth", "access": "a"}}},  # no refresh
    ],
)
def test_misses_return_none(tmp_path, store):
    db = _write_store_db(tmp_path / "a.sqlite", store)
    assert _read_live_profile_from_db(db) is None


def test_missing_table_returns_none(tmp_path):
    db = tmp_path / "bare.sqlite"
    sqlite3.connect(str(db)).close()
    assert _read_live_profile_from_db(str(db)) is None


# --- _live_profile_is_fresher ---------------------------------------------------


def test_same_refresh_token_is_not_fresher():
    # Container never rotated past the seed — nothing to write.
    assert _live_profile_is_fresher(_profile(refresh="rt-seed"), {"refresh_token": "rt-seed"}) is False


def test_rotated_chain_with_newer_expiry_is_fresher():
    assert _live_profile_is_fresher(_profile(expires=2_000_000), {"refresh_token": "rt-seed", "expires_at": 1_000_000})


def test_lagged_replica_loses_to_newer_reconnect_grant():
    # User re-ran the device-code flow: the DDB grant postdates the old chain.
    assert (
        _live_profile_is_fresher(
            _profile(expires=1_000_000), {"refresh_token": "rt-new-grant", "expires_at": 2_000_000}
        )
        is False
    )


def test_pre_expires_at_seed_row_loses_to_any_rotation():
    assert _live_profile_is_fresher(_profile(expires=1), {"refresh_token": "rt-seed"}) is True


# --- harvest_rotated_oauth_seed --------------------------------------------------


def _patch_restore(monkeypatch, tmp_path, store) -> None:
    db = _write_store_db(tmp_path / "restored.sqlite", store)
    monkeypatch.setattr(oauth_service, "_restore_replica_db", lambda bucket, owner_id, dest_dir: db)


def _patch_personal_connector(monkeypatch) -> None:
    async def _no_billing_row(owner_id):
        return None  # personal owner: connector falls back to owner_id

    monkeypatch.setattr(oauth_service.billing_repo, "get_by_owner_id", _no_billing_row)


@pytest.mark.asyncio
async def test_harvest_writes_rotated_tokens_back(oauth_table, tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "b", raising=False)
    _patch_personal_connector(monkeypatch)
    _seed_active_row("u_1")
    _patch_restore(monkeypatch, tmp_path, {"version": 1, "profiles": {"openai:default": _profile()}})

    assert await harvest_rotated_oauth_seed(owner_id="u_1") is True

    tokens = _read_row_tokens("u_1")
    assert tokens["refresh_token"] == "rt-rotated"
    assert tokens["access_token"] == "at-rotated"
    assert tokens["expires_at"] == 2_000_000
    # Identity fields keep their connect-time values.
    assert tokens["id_token"] == "idt-seed"
    assert tokens["account_id"] == "acc-1"


@pytest.mark.asyncio
async def test_harvest_noop_when_seed_is_current(oauth_table, tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "b", raising=False)
    _patch_personal_connector(monkeypatch)
    _seed_active_row("u_1", refresh="rt-rotated", expires_at=2_000_000)
    _patch_restore(monkeypatch, tmp_path, {"version": 1, "profiles": {"openai:default": _profile()}})

    assert await harvest_rotated_oauth_seed(owner_id="u_1") is False
    assert _read_row_tokens("u_1")["access_token"] == "at-seed"  # untouched


@pytest.mark.asyncio
async def test_harvest_noop_without_bucket(oauth_table, monkeypatch):
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "", raising=False)
    called = []
    monkeypatch.setattr(oauth_service, "_restore_replica_db", lambda *a: called.append(a))
    assert await harvest_rotated_oauth_seed(owner_id="u_1") is False
    assert not called


@pytest.mark.asyncio
async def test_harvest_noop_when_no_replica(oauth_table, monkeypatch):
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "b", raising=False)
    monkeypatch.setattr(oauth_service, "_restore_replica_db", lambda *a: None)
    assert await harvest_rotated_oauth_seed(owner_id="u_1") is False


@pytest.mark.asyncio
async def test_harvest_skips_row_bound_to_another_workspace(oauth_table, tmp_path, monkeypatch):
    """The replica belongs to owner_id's container; a row the connector bound
    to a DIFFERENT workspace must not be overwritten with this one's chain."""
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "b", raising=False)
    _patch_personal_connector(monkeypatch)
    _seed_active_row("u_1", bound_owner_id="org_other")
    _patch_restore(monkeypatch, tmp_path, {"version": 1, "profiles": {"openai:default": _profile()}})

    assert await harvest_rotated_oauth_seed(owner_id="u_1") is False
    assert _read_row_tokens("u_1")["refresh_token"] == "rt-seed"


@pytest.mark.asyncio
async def test_harvest_resolves_org_connector_row(oauth_table, tmp_path, monkeypatch):
    """Org replica prefix is keyed by org_id; the token row lives under the
    connector's Clerk user id, bound to the org."""
    monkeypatch.setattr(oauth_service.settings, "LITESTREAM_BUCKET", "b", raising=False)

    async def _org_billing_row(owner_id):
        return {"oauth_connector_user_id": "user_admin"}

    monkeypatch.setattr(oauth_service.billing_repo, "get_by_owner_id", _org_billing_row)
    _seed_active_row("user_admin", bound_owner_id="org_1")
    _patch_restore(monkeypatch, tmp_path, {"version": 1, "profiles": {"openai:default": _profile()}})

    assert await harvest_rotated_oauth_seed(owner_id="org_1") is True
    assert _read_row_tokens("user_admin")["refresh_token"] == "rt-rotated"
