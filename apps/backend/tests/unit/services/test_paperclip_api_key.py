"""Unit tests for paperclip_api_key.ensure_paperclip_api_key.

Covers the mint-or-read flow: an existing Secrets Manager secret is read
back and returned verbatim; a miss triggers a Paperclip
create_agent_api_key call followed by a Secrets Manager create_secret. Every
failure path swallows + returns None so a Paperclip (or Secrets Manager)
outage never blocks container provision.

Storage moved from the api-keys DDB table (Fernet-encrypted) to AWS Secrets
Manager (plaintext SecretString) in the credential-layer redesign, so these
tests patch ``boto3.client`` with an in-memory FakeSM rather than mocking
``api_key_repo`` + encrypt/decrypt.
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


def _secret_name() -> str:
    return paperclip_api_key._secret_name("user_x", "friday-uuid")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mints_and_stores_when_no_existing_key(admin_client, fake_sm):
    """SM miss → mint via Paperclip → create_secret stores the token."""
    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result == "pcli_live_abc"
    admin_client.create_agent_api_key.assert_awaited_once()
    # Idempotency key includes both owner_id and agent_id so retries
    # collapse to the same Paperclip record.
    assert admin_client.create_agent_api_key.await_args.kwargs["idempotency_key"] == "user_x:mcp-key:friday-uuid"
    # Plaintext token stored in Secrets Manager under the namespaced name.
    name = _secret_name()
    assert fake_sm.store[name] == "pcli_live_abc"
    assert fake_sm.create_calls == [(name, "pcli_live_abc")]
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_a_racing_writer_wins_and_our_duplicate_is_revoked(admin_client, fake_sm):
    """SM read misses (store empty), so we mint; but a concurrent provision has
    stored ITS key by the time create_secret runs. Create-only: the stored key
    wins, we return it, and the key we just minted is revoked best-effort so the
    agent is not left holding two live credentials."""
    name = _secret_name()
    admin_client.revoke_agent_api_key = AsyncMock(return_value={})

    real_create = fake_sm.create_secret

    def racing_create(*, Name, SecretString):
        fake_sm.store[Name] = "pcli_live_from_racer"
        return real_create(Name=Name, SecretString=SecretString)

    fake_sm.create_secret = racing_create

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result == "pcli_live_from_racer"
    assert fake_sm.put_calls == []
    assert fake_sm.store[name] == "pcli_live_from_racer"
    admin_client.revoke_agent_api_key.assert_awaited_once_with(
        session_cookie="session=admin", agent_id="friday-uuid", key_id="key-1"
    )


@pytest.mark.asyncio
async def test_a_failed_revoke_of_the_duplicate_still_returns_the_winner(admin_client, fake_sm):
    admin_client.revoke_agent_api_key = AsyncMock(side_effect=httpx.ConnectError("down"))
    fake_sm.store[_secret_name()] = "pcli_live_from_racer"
    fake_sm.get_secret_value = MagicMock(
        side_effect=[FakeSM.ResourceNotFoundException("miss"), {"SecretString": "pcli_live_from_racer"}]
    )

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result == "pcli_live_from_racer"


@pytest.mark.asyncio
async def test_reads_existing_key_without_calling_paperclip(admin_client, fake_sm):
    """SM hit → return stored token, never touch Paperclip."""
    fake_sm.store[_secret_name()] = "pcli_live_stored"

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result == "pcli_live_stored"
    # No mint — Paperclip wasn't called and nothing was written.
    admin_client.create_agent_api_key.assert_not_awaited()
    assert fake_sm.create_calls == []
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_second_call_is_idempotent_and_hits_sm_not_paperclip(admin_client, fake_sm):
    """First call mints + stores; a second call reads SM and skips Paperclip."""
    first = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )
    assert first == "pcli_live_abc"
    admin_client.create_agent_api_key.assert_awaited_once()

    second = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )
    assert second == "pcli_live_abc"
    # Still only one mint across both calls — the second read short-circuits.
    admin_client.create_agent_api_key.assert_awaited_once()
    # Only one create_secret total (from the first call).
    assert len(fake_sm.create_calls) == 1


# ---------------------------------------------------------------------------
# Failure paths — all return None, none raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_admin_session_fails(admin_client, fake_sm, monkeypatch):
    """RuntimeError from get_admin_session_cookie (Secrets Manager miss /
    malformed payload) is swallowed; we return None so provision proceeds."""

    async def fail_cookie(_http):
        raise RuntimeError("secrets manager unavailable")

    monkeypatch.setattr(paperclip_api_key, "get_admin_session_cookie", fail_cookie)

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    admin_client.create_agent_api_key.assert_not_awaited()
    assert fake_sm.create_calls == []


@pytest.mark.asyncio
async def test_returns_none_when_paperclip_5xxs(admin_client, fake_sm):
    admin_client.create_agent_api_key = AsyncMock(side_effect=PaperclipApiError("upstream down", 503, None))

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    assert fake_sm.create_calls == []
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_returns_none_when_transport_fails(admin_client, fake_sm):
    admin_client.create_agent_api_key = AsyncMock(side_effect=httpx.ConnectError("upstream down"))

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    assert fake_sm.create_calls == []
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_returns_none_when_paperclip_response_missing_token(admin_client, fake_sm):
    admin_client.create_agent_api_key = AsyncMock(
        return_value={"id": "key-1", "name": "lightbulb-mcp-server"},
    )  # no "token" field

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    assert fake_sm.create_calls == []
    assert fake_sm.put_calls == []


@pytest.mark.asyncio
async def test_sm_read_error_degrades_to_remint(admin_client, fake_sm):
    """A non-ResourceNotFound read error (transient SM outage) is swallowed
    and treated as 'no key', so we fall through to a fresh mint + store."""

    def boom(*, SecretId):
        raise RuntimeError("secretsmanager read timeout")

    fake_sm.get_secret_value = boom

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    # Read error → re-mint path → token returned + stored.
    assert result == "pcli_live_abc"
    admin_client.create_agent_api_key.assert_awaited_once()
    assert fake_sm.store[_secret_name()] == "pcli_live_abc"


@pytest.mark.asyncio
async def test_returns_none_when_sm_store_fails(admin_client, fake_sm):
    """A Secrets Manager write error after a successful mint is swallowed —
    Paperclip has the key but we return None so the next pass re-mints."""

    def boom(*, Name, SecretString):
        raise RuntimeError("secretsmanager unavailable")

    fake_sm.create_secret = boom

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    # We did mint (Paperclip was called) but the store failed.
    admin_client.create_agent_api_key.assert_awaited_once()


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
