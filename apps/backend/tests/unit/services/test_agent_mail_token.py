"""agent_mail_token: tamper-proof reply-context round-trip."""

import json
import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "0" * 44)  # Fernet: 32 url-safe-base64 bytes

from core.services import agent_mail_token  # noqa: E402
from core.services.agent_mail_token import encrypt  # noqa: E402 — same import the module uses


def _ctx():
    return {
        "owner_id": "org_1",
        "agent_id": "agent_friday",
        "reply_to": "alice@example.com",
        "in_reply_to": "<msg-123@mail.example.com>",
        "subject": "Re: hello",
    }


def test_round_trip():
    token = agent_mail_token.encode_reply_token(_ctx())
    assert isinstance(token, str) and token
    assert agent_mail_token.decode_reply_token(token) == _ctx()


def test_token_is_opaque():
    token = agent_mail_token.encode_reply_token(_ctx())
    # No plaintext address leaks into the token.
    assert "alice@example.com" not in token


def test_decode_rejects_tampered_token():
    token = agent_mail_token.encode_reply_token(_ctx())
    assert agent_mail_token.decode_reply_token(token[:-4] + "AAAA") is None


def test_decode_rejects_mid_payload_tamper():
    # Flip a character in the MIDDLE of the token (not the tail) — this
    # exercises Fernet's HMAC authentication specifically. A naive
    # base64+JSON codec (no auth) would only be caught by tail-mangling
    # tests like the one above; a mid-payload flip proves the codec
    # actually authenticates the whole ciphertext, not just its shape.
    token = agent_mail_token.encode_reply_token(_ctx())
    mid = len(token) // 2
    flipped_char = "A" if token[mid] != "A" else "B"
    tampered = token[:mid] + flipped_char + token[mid + 1 :]
    assert agent_mail_token.decode_reply_token(tampered) is None


def test_decode_rejects_garbage():
    assert agent_mail_token.decode_reply_token("not-a-token") is None
    assert agent_mail_token.decode_reply_token("") is None


def test_decode_rejects_wrong_shape_dict():
    # A genuinely-decryptable token (same Fernet key, valid HMAC) whose
    # plaintext is JSON but not the expected ReplyContext shape.
    token = encrypt(json.dumps({"totally": "different"}))
    assert agent_mail_token.decode_reply_token(token) is None


def test_decode_rejects_wrong_shape_list():
    token = encrypt(json.dumps([1, 2, 3]))
    assert agent_mail_token.decode_reply_token(token) is None


def test_decode_rejects_wrong_shape_string():
    token = encrypt(json.dumps("hi"))
    assert agent_mail_token.decode_reply_token(token) is None


def test_decode_valid_round_trip_still_returns_full_context():
    # Guard against over-tightening: a well-formed token must still
    # decode to the full context, not just "truthy".
    token = agent_mail_token.encode_reply_token(_ctx())
    ctx = agent_mail_token.decode_reply_token(token)
    assert ctx == _ctx()
    assert set(ctx.keys()) == {
        "owner_id",
        "agent_id",
        "reply_to",
        "in_reply_to",
        "subject",
    }


# --- mint_first_contact_token -------------------------------------------------


@pytest.mark.asyncio
async def test_mint_first_contact_token_active_row_present(monkeypatch):
    monkeypatch.setattr(agent_mail_token.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_token.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "friday@agents.isol8.co", "agent_id": "friday-uuid", "status": "active"},
            ]
        ),
    )

    token = await agent_mail_token.mint_first_contact_token(
        owner_id="org_1",
        owner_email="owner@acme.com",
        subject="Getting started with Acme",
    )

    assert isinstance(token, str) and token
    ctx = agent_mail_token.decode_reply_token(token)
    assert ctx["reply_to"] == "owner@acme.com"
    assert ctx["in_reply_to"] == ""
    assert ctx["subject"] == "Getting started with Acme"
    assert ctx["owner_id"] == "org_1"
    assert ctx["agent_id"] == "friday-uuid"


@pytest.mark.asyncio
async def test_mint_first_contact_token_domain_unset_returns_none(monkeypatch):
    monkeypatch.setattr(agent_mail_token.settings, "AGENT_EMAIL_DOMAIN", "")
    query_mock = AsyncMock()
    monkeypatch.setattr(agent_mail_token.agent_email_repo, "query_by_owner", query_mock)

    token = await agent_mail_token.mint_first_contact_token(
        owner_id="org_1", owner_email="owner@acme.com", subject="Hello"
    )

    assert token is None
    query_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_mint_first_contact_token_no_active_row_returns_none(monkeypatch):
    monkeypatch.setattr(agent_mail_token.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_token.agent_email_repo,
        "query_by_owner",
        AsyncMock(
            return_value=[
                {"address": "old@agents.isol8.co", "agent_id": "friday-uuid", "status": "alias"},
            ]
        ),
    )

    token = await agent_mail_token.mint_first_contact_token(
        owner_id="org_1", owner_email="owner@acme.com", subject="Hello"
    )

    assert token is None


@pytest.mark.asyncio
async def test_mint_first_contact_token_no_rows_at_all_returns_none(monkeypatch):
    monkeypatch.setattr(agent_mail_token.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_token.agent_email_repo,
        "query_by_owner",
        AsyncMock(return_value=[]),
    )

    token = await agent_mail_token.mint_first_contact_token(
        owner_id="org_1", owner_email="owner@acme.com", subject="Hello"
    )

    assert token is None


@pytest.mark.asyncio
async def test_mint_first_contact_token_never_raises_on_repo_error(monkeypatch):
    monkeypatch.setattr(agent_mail_token.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    monkeypatch.setattr(
        agent_mail_token.agent_email_repo,
        "query_by_owner",
        AsyncMock(side_effect=RuntimeError("dynamodb throttled")),
    )

    token = await agent_mail_token.mint_first_contact_token(
        owner_id="org_1", owner_email="owner@acme.com", subject="Hello"
    )

    assert token is None
