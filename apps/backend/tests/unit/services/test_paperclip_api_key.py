"""Unit tests for paperclip_api_key.ensure_paperclip_api_key.

Covers the mint-or-read flow: existing rows are decrypted and returned;
missing rows trigger a Paperclip create_agent_api_key call + Fernet
encrypt + DDB write. Every failure path swallows + returns None so a
Paperclip outage never blocks container provision.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import paperclip_api_key
from core.services.paperclip_admin_client import PaperclipApiError


@pytest.fixture
def admin_client(monkeypatch):
    """Mock PaperclipAdminClient + auto-resolving admin session cookie."""
    client = MagicMock()
    client._http = MagicMock()
    client.create_agent_api_key = AsyncMock(
        return_value={"id": "key-1", "name": "isol8-mcp-server", "token": "pcli_live_abc"}
    )

    async def fake_cookie(_http):
        return "session=admin"

    monkeypatch.setattr(paperclip_api_key, "get_admin_session_cookie", fake_cookie)
    return client


@pytest.fixture
def encryption(monkeypatch):
    """Round-trip encrypt/decrypt that's reversible without a real Fernet
    key so tests don't need ENCRYPTION_KEY set."""

    def fake_encrypt(plaintext: str) -> str:
        return f"enc:{plaintext}"

    def fake_decrypt(ciphertext: str) -> str:
        assert ciphertext.startswith("enc:")
        return ciphertext[4:]

    monkeypatch.setattr(paperclip_api_key, "encrypt", fake_encrypt)
    monkeypatch.setattr(paperclip_api_key, "decrypt", fake_decrypt)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mints_and_stores_when_no_existing_key(admin_client, encryption, monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value=None)
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)

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
    # Encrypted plaintext stored under the namespaced tool_id.
    fake_repo.set_key.assert_awaited_once()
    set_kwargs = fake_repo.set_key.await_args.kwargs
    assert set_kwargs["user_id"] == "user_x"
    assert set_kwargs["tool_id"] == "paperclip-agent-key:friday-uuid"
    assert set_kwargs["encrypted_key"] == "enc:pcli_live_abc"


@pytest.mark.asyncio
async def test_reads_existing_key_without_calling_paperclip(admin_client, encryption, monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(
        return_value={
            "user_id": "user_x",
            "tool_id": "paperclip-agent-key:friday-uuid",
            "encrypted_key": "enc:pcli_live_stored",
        }
    )
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result == "pcli_live_stored"
    # No mint — Paperclip wasn't called.
    admin_client.create_agent_api_key.assert_not_awaited()
    fake_repo.set_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# Failure paths — all return None, none raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_admin_session_fails(admin_client, encryption, monkeypatch):
    """RuntimeError from get_admin_session_cookie (Secrets Manager miss /
    malformed payload) is swallowed; we return None so provision proceeds."""
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value=None)
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)

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


@pytest.mark.asyncio
async def test_returns_none_when_paperclip_5xxs(admin_client, encryption, monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value=None)
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)
    admin_client.create_agent_api_key = AsyncMock(side_effect=PaperclipApiError("upstream down", 503, None))

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    fake_repo.set_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_when_transport_fails(admin_client, encryption, monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value=None)
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)
    admin_client.create_agent_api_key = AsyncMock(side_effect=httpx.ConnectError("upstream down"))

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    fake_repo.set_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_returns_none_when_paperclip_response_missing_token(admin_client, encryption, monkeypatch):
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value=None)
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)
    admin_client.create_agent_api_key = AsyncMock(
        return_value={"id": "key-1", "name": "isol8-mcp-server"},
    )  # no "token" field

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    assert result is None
    fake_repo.set_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_stored_key_falls_through_to_remint(admin_client, encryption, monkeypatch):
    """An undecryptable row is treated as 'no key' and re-minted. We do
    NOT delete the row on the read path — if Fernet has been rotated and
    just needs a re-init, the next read might succeed."""
    fake_repo = MagicMock()
    fake_repo.get_key = AsyncMock(return_value={"encrypted_key": "not-prefixed-with-enc:"})
    fake_repo.set_key = AsyncMock()
    monkeypatch.setattr(paperclip_api_key, "api_key_repo", fake_repo)

    result = await paperclip_api_key.ensure_paperclip_api_key(
        owner_id="user_x",
        paperclip_agent_id="friday-uuid",
        admin_client=admin_client,
    )

    # Corrupt-then-mint: we got a fresh token from Paperclip
    assert result == "pcli_live_abc"
    admin_client.create_agent_api_key.assert_awaited_once()
