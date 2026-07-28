"""agent_mail_token: tamper-proof reply-context round-trip."""

import json
import os

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


def test_token_without_references_still_decodes():
    """`references` was added after tokens were already in flight inside
    container conversations. It must stay OUT of the required-key set: making
    it required would decode every pre-existing token as None and silently
    break replies to any thread already in progress."""
    from core.services.agent_mail_token import decode_reply_token, encode_reply_token

    legacy = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<m@x>",
        "subject": "Need help",
    }
    decoded = decode_reply_token(encode_reply_token(legacy))
    assert decoded is not None
    assert decoded.get("references", "") == ""


def test_token_roundtrips_references_when_present():
    from core.services.agent_mail_token import decode_reply_token, encode_reply_token

    ctx = {
        "owner_id": "o",
        "agent_id": "a1",
        "reply_to": "alice@x.com",
        "in_reply_to": "<b@x>",
        "references": "<a@x>",
        "subject": "Need help",
    }
    decoded = decode_reply_token(encode_reply_token(ctx))
    assert decoded is not None
    assert decoded["references"] == "<a@x>"
