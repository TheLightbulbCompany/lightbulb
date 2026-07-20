"""verify_svix_signature: HMAC-SHA256 over id.timestamp.body, Svix format."""

import base64
import hashlib
import hmac
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.crypto.svix import verify_svix_signature  # noqa: E402

_SECRET = "whsec_" + base64.b64encode(b"0" * 24).decode()


def _sign(body: bytes, msg_id: str, ts: str) -> str:
    key = base64.b64decode(_SECRET.removeprefix("whsec_"))
    signed = f"{msg_id}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{sig}"


def _headers(body: bytes, msg_id="msg_1", ts="1700000000"):
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": _sign(body, msg_id, ts),
    }


def test_valid_signature():
    body = b'{"hello":"world"}'
    assert verify_svix_signature(secret=_SECRET, body=body, headers=_headers(body)) is True


def test_wrong_body_fails():
    body = b'{"hello":"world"}'
    assert verify_svix_signature(secret=_SECRET, body=b'{"tampered":true}', headers=_headers(body)) is False


def test_missing_headers_fail():
    assert verify_svix_signature(secret=_SECRET, body=b"x", headers={}) is False


def test_multiple_signature_tokens_one_valid():
    body = b"x"
    good = _sign(body, "msg_1", "1700000000")
    hdrs = _headers(body)
    hdrs["svix-signature"] = f"v1,badbadbad {good}"  # space-separated list; one valid
    assert verify_svix_signature(secret=_SECRET, body=body, headers=hdrs) is True
