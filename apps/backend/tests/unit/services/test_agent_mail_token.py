"""agent_mail_token: tamper-proof reply-context round-trip."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "0" * 44)  # Fernet: 32 url-safe-base64 bytes

from core.services import agent_mail_token  # noqa: E402


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


def test_decode_rejects_garbage():
    assert agent_mail_token.decode_reply_token("not-a-token") is None
    assert agent_mail_token.decode_reply_token("") is None
