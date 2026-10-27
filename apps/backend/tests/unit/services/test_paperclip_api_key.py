"""Unit tests for paperclip_api_key.ensure_paperclip_api_key.

Covers the mint-or-read flow: the workspace row is read first, the legacy
Secrets Manager secret second; a miss on both mints via Paperclip and stores
on the row only (no secret is ever created). Every failure path swallows +
returns None so a Paperclip (or storage) outage never blocks provision.
``FakeRepo`` stands in for ``PaperclipRepo`` (its moto round-trip lives in
``tests/unit/repositories/test_paperclip_repo.py``); ``FakeSM`` for boto3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import paperclip_api_key
from core.services.paperclip_admin_client import PaperclipApiError


class FakeSM:
    """In-memory Secrets Manager double.

    Backed by ``store`` keyed by secret name. Exposes the subset of the
    boto3 secretsmanager surface paperclip_api_key uses
    (get_secret_value / create_secret / put_secret_value) plus the nested
    ``.exceptions`` namespace it pattern-matches on.
    """

    class ResourceNotFoundException(Exception):
        pass

    class ResourceExistsException(Exception):
        pass

    def __init__(self, store: dict[str, str]):
        self.store = store
        # boto3 exposes exception classes on the client instance.
        self.exceptions = MagicMock()
        self.exceptions.ResourceNotFoundException = FakeSM.ResourceNotFoundException
        self.exceptions.ResourceExistsException = FakeSM.ResourceExistsException
        # Spies so tests can assert which path ran.
        self.create_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get_secret_value(self, *, SecretId: str):
        self.get_calls.append(SecretId)
        if SecretId not in self.store:
            raise FakeSM.ResourceNotFoundException(SecretId)
        return {"SecretString": self.store[SecretId]}

    def create_secret(self, *, Name: str, SecretString: str):
        self.create_calls.append((Name, SecretString))
        if Name in self.store:
            raise FakeSM.ResourceExistsException(Name)
        self.store[Name] = SecretString
        return {"Name": Name}

    def put_secret_value(self, *, SecretId: str, SecretString: str):
        self.put_calls.append((SecretId, SecretString))
        self.store[SecretId] = SecretString
        return {"SecretId": SecretId}

    class _Paginator:
        """Honors the ``name`` prefix filter like real Secrets Manager."""

        def __init__(self, store: dict[str, str]):
            self._store = store

        def paginate(self, Filters=None):
            prefix = ""
            for f in Filters or []:
                if f.get("Key") == "name" and f.get("Values"):
                    prefix = f["Values"][0]
            names = [n for n in self._store if n.startswith(prefix)]
            yield {"SecretList": [{"Name": n} for n in names]}

    def get_paginator(self, operation: str):
        assert operation == "list_secrets"
        return FakeSM._Paginator(self.store)

    def delete_secret(self, *, SecretId: str, ForceDeleteWithoutRecovery: bool = False):
        self.delete_calls.append(SecretId)
        if SecretId not in self.store:
            raise FakeSM.ResourceNotFoundException(SecretId)
        del self.store[SecretId]
        return {"Name": SecretId}


@pytest.fixture
def sm_store() -> dict[str, str]:
    """Shared backing store so a single FakeSM instance is reused across
    the get→create round-trip within one ensure_paperclip_api_key call."""
    return {}


@pytest.fixture
def fake_sm(sm_store, monkeypatch) -> FakeSM:
    """Patch ``boto3.client`` to return one stateful FakeSM regardless of
    how many times the source constructs a client."""
    sm = FakeSM(sm_store)
    monkeypatch.setattr(paperclip_api_key.boto3, "client", lambda *a, **k: sm)
    return sm


@pytest.fixture
def admin_client(monkeypatch):
    """Mock PaperclipAdminClient + auto-resolving admin session cookie."""
    client = MagicMock()
    client._http = MagicMock()
    client.create_agent_api_key = AsyncMock(
        return_value={"id": "key-1", "name": "lightbulb-mcp-server", "token": "pcli_live_abc"}
    )

    async def fake_cookie(_http):
        return "session=admin"

    monkeypatch.setattr(paperclip_api_key, "get_admin_session_cookie", fake_cookie)
    return client


class FakeRepo:
    """In-memory ``PaperclipRepo`` agent-key surface (create-only put, like the real one)."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self.put_calls: list[tuple[str, str]] = []

    async def get_agent_key(self, org_id, agent_id):
        return self.store.get((org_id, agent_id))

    async def put_agent_key(self, org_id, agent_id, plaintext):
        self.put_calls.append((org_id, agent_id))
        return self.store.setdefault((org_id, agent_id), plaintext)


@pytest.fixture
def fake_repo(monkeypatch) -> FakeRepo:
    repo = FakeRepo()
    monkeypatch.setattr(paperclip_api_key, "_repo", lambda: repo)
    return repo


def _secret_name() -> str:
    return paperclip_api_key._secret_name("user_x", "friday-uuid")


KEY = ("user_x", "friday-uuid")


async def _ensure(admin_client):
    return await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x", paperclip_agent_id="friday-uuid", admin_client=admin_client
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mints_and_stores_on_the_row_and_creates_no_secret(admin_client, fake_sm, fake_repo):
    """Row miss + SM miss -> mint via Paperclip -> the key lands on the row; no secret is created."""
    result = await _ensure(admin_client)

    assert result == "pcli_live_abc"
    # Idempotency key includes both owner_id and agent_id so retries collapse to one Paperclip record.
    assert admin_client.create_agent_api_key.await_args.kwargs["idempotency_key"] == "user_x:mcp-key:friday-uuid"
    assert fake_repo.store[KEY] == "pcli_live_abc"
    assert fake_sm.create_calls == []
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_row_is_read_first_and_secrets_manager_is_not_touched(admin_client, fake_sm, fake_repo):
    fake_repo.store[KEY] = "pcli_live_row"
    fake_sm.store[_secret_name()] = "pcli_live_legacy"

    assert await _ensure(admin_client) == "pcli_live_row"
    assert fake_sm.get_calls == []
    admin_client.create_agent_api_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_row_miss_falls_back_to_the_legacy_secret_without_minting(admin_client, fake_sm, fake_repo):
    fake_sm.store[_secret_name()] = "pcli_live_legacy"

    assert await _ensure(admin_client) == "pcli_live_legacy"
    admin_client.create_agent_api_key.assert_not_awaited()
    assert fake_repo.put_calls == []
    assert fake_sm.create_calls == []


@pytest.mark.asyncio
async def test_a_row_read_error_still_falls_back_to_the_legacy_secret(admin_client, fake_sm, fake_repo):
    fake_repo.get_agent_key = AsyncMock(side_effect=RuntimeError("dynamodb down"))
    fake_sm.store[_secret_name()] = "pcli_live_legacy"

    assert await _ensure(admin_client) == "pcli_live_legacy"


@pytest.mark.asyncio
async def test_a_racing_writer_wins_and_our_duplicate_is_revoked(admin_client, fake_sm, fake_repo):
    """Both reads miss, so we mint; a concurrent provision stored ITS key first. Create-only:
    the stored key wins and the key we minted is revoked."""
    admin_client.revoke_agent_api_key = AsyncMock(return_value={})
    real_put = fake_repo.put_agent_key

    async def racing_put(org_id, agent_id, plaintext):
        fake_repo.store[(org_id, agent_id)] = "pcli_live_from_racer"
        return await real_put(org_id, agent_id, plaintext)

    fake_repo.put_agent_key = racing_put

    assert await _ensure(admin_client) == "pcli_live_from_racer"
    assert fake_repo.store[KEY] == "pcli_live_from_racer"
    admin_client.revoke_agent_api_key.assert_awaited_once_with(
        session_cookie="session=admin", agent_id="friday-uuid", key_id="key-1"
    )


@pytest.mark.asyncio
async def test_a_failed_revoke_of_the_duplicate_still_returns_the_winner(admin_client, fake_sm, fake_repo):
    admin_client.revoke_agent_api_key = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_repo.put_agent_key = AsyncMock(return_value="pcli_live_from_racer")

    assert await _ensure(admin_client) == "pcli_live_from_racer"


@pytest.mark.asyncio
async def test_second_call_is_idempotent_and_reads_the_row(admin_client, fake_sm, fake_repo):
    assert await _ensure(admin_client) == "pcli_live_abc"
    assert await _ensure(admin_client) == "pcli_live_abc"
    admin_client.create_agent_api_key.assert_awaited_once()
    assert fake_sm.create_calls == []


# ---------------------------------------------------------------------------
# Failure paths — all return None, none raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_admin_session_fails(admin_client, fake_sm, fake_repo, monkeypatch):
    async def fail_cookie(_http):
        raise RuntimeError("secrets manager unavailable")

    monkeypatch.setattr(paperclip_api_key, "get_admin_session_cookie", fail_cookie)

    assert await _ensure(admin_client) is None
    admin_client.create_agent_api_key.assert_not_awaited()
    assert fake_repo.put_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PaperclipApiError("upstream down", 503, None), httpx.ConnectError("upstream down")])
async def test_returns_none_when_the_mint_fails(admin_client, fake_sm, fake_repo, error):
    admin_client.create_agent_api_key = AsyncMock(side_effect=error)

    assert await _ensure(admin_client) is None
    assert fake_repo.put_calls == []


@pytest.mark.asyncio
async def test_returns_none_when_paperclip_response_missing_token(admin_client, fake_sm, fake_repo):
    admin_client.create_agent_api_key = AsyncMock(return_value={"id": "key-1", "name": "lightbulb-mcp-server"})

    assert await _ensure(admin_client) is None
    assert fake_repo.put_calls == []


@pytest.mark.asyncio
async def test_sm_read_error_degrades_to_remint_onto_the_row(admin_client, fake_sm, fake_repo):
    def boom(*, SecretId):
        raise RuntimeError("secretsmanager read timeout")

    fake_sm.get_secret_value = boom

    assert await _ensure(admin_client) == "pcli_live_abc"
    assert fake_repo.store[KEY] == "pcli_live_abc"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AsyncMock(return_value=None), AsyncMock(side_effect=RuntimeError("ddb"))])
async def test_returns_none_when_the_row_store_fails(admin_client, fake_sm, fake_repo, failure):
    """No workspace row (put returns None) or a write error: return None, never write a secret."""
    fake_repo.put_agent_key = failure

    assert await _ensure(admin_client) is None
    admin_client.create_agent_api_key.assert_awaited_once()
    assert fake_sm.create_calls == []


# ---------------------------------------------------------------------------
# Per-owner agent-key sweep (Admin Account-Wipe support)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_owner_agent_key_secrets_scopes_to_owner_prefix(fake_sm):
    """Lists ONLY the target owner's per-agent key secrets — never another
    owner's, and never a different secret namespace."""
    px = paperclip_api_key._owner_prefix("user_x")
    other = paperclip_api_key._owner_prefix("user_OTHER")
    fake_sm.store.update(
        {
            f"{px}a1": "k1",
            f"{px}a2": "k2",
            f"{other}a1": "decoy-other-owner",
            "lightbulb/dev/user-keys/user_x/openai": "decoy-other-namespace",
        }
    )

    names = await paperclip_api_key.list_owner_agent_key_secret_names("user_x")

    assert sorted(names) == sorted([f"{px}a1", f"{px}a2"])


@pytest.mark.asyncio
async def test_delete_owner_agent_key_secrets_deletes_only_owner_and_counts(fake_sm):
    px = paperclip_api_key._owner_prefix("user_x")
    other = paperclip_api_key._owner_prefix("user_OTHER")
    fake_sm.store.update({f"{px}a1": "k1", f"{px}a2": "k2", f"{other}a1": "survivor"})

    count = await paperclip_api_key.delete_owner_agent_key_secrets("user_x")

    assert count == 2
    assert sorted(fake_sm.delete_calls) == sorted([f"{px}a1", f"{px}a2"])
    # The other owner's key must survive the sweep.
    assert f"{other}a1" in fake_sm.store
    assert not any(n.startswith(px) for n in fake_sm.store)


@pytest.mark.asyncio
async def test_delete_owner_agent_key_secrets_noops_when_none(fake_sm):
    count = await paperclip_api_key.delete_owner_agent_key_secrets("user_with_no_keys")
    assert count == 0
    assert fake_sm.delete_calls == []
