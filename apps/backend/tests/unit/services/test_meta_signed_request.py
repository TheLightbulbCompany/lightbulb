"""meta_signed_request.verify — the only gate on the unauthenticated Meta
webhooks, so every failure mode must raise rather than return a payload."""

import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from core.services import meta_signed_request
from core.services.meta_signed_request import SignatureError

SECRET = "app-secret-value"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(encoded_payload: str, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
    return _b64(digest)


def _make(payload: dict, secret: str = SECRET) -> str:
    encoded = _b64(json.dumps(payload).encode())
    return f"{_sign(encoded, secret)}.{encoded}"


@pytest.fixture(autouse=True)
def _app(monkeypatch):
    monkeypatch.setattr(
        meta_signed_request.platform_oauth_app_store,
        "get",
        AsyncMock(return_value={"client_id": "cid", "client_secret": SECRET}),
    )


VALID = {"algorithm": "HMAC-SHA256", "user_id": "17841400000000000", "issued_at": 1}


@pytest.mark.asyncio
async def test_valid_signed_request_returns_payload():
    got = await meta_signed_request.verify(_make(VALID), "meta-instagram")
    assert got["user_id"] == "17841400000000000"


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected():
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(_make(VALID, secret="not-our-secret"), "meta-instagram")


@pytest.mark.asyncio
async def test_tampered_payload_is_rejected():
    """The classic attack: keep a captured signature, swap in another user id."""
    good = _make(VALID)
    sig = good.split(".")[0]
    forged = _b64(json.dumps({**VALID, "user_id": "victim"}).encode())
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(f"{sig}.{forged}", "meta-instagram")


@pytest.mark.asyncio
async def test_algorithm_none_downgrade_is_rejected():
    """`algorithm` is attacker-supplied; honouring it would be alg:none."""
    payload = {**VALID, "algorithm": "none"}
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(_make(payload), "meta-instagram")


@pytest.mark.asyncio
async def test_signature_covers_the_encoded_payload_not_the_json():
    """Signing re-serialised JSON must NOT verify — key order and separators
    are not stable, so accepting it would make valid requests fail in prod."""
    encoded = _b64(json.dumps(VALID).encode())
    over_decoded = _b64(hmac.new(SECRET.encode(), json.dumps(VALID).encode(), hashlib.sha256).digest())
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(f"{over_decoded}.{encoded}", "meta-instagram")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["", "no-dot-separator", ".", "abc.", ".abc", "a.b.c", "!!!.@@@"],
)
async def test_malformed_input_raises(value):
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(value, "meta-instagram")


@pytest.mark.asyncio
async def test_unprovisioned_app_secret_fails_closed(monkeypatch):
    """No secret must never read as 'valid' — that would let anyone POST a
    teardown for any user."""
    monkeypatch.setattr(meta_signed_request.platform_oauth_app_store, "get", AsyncMock(return_value=None))
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(_make(VALID), "meta-instagram")


@pytest.mark.asyncio
async def test_non_json_payload_raises():
    encoded = _b64(b"not json at all")
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(f"{_sign(encoded)}.{encoded}", "meta-instagram")


@pytest.mark.asyncio
async def test_non_object_payload_raises():
    encoded = _b64(json.dumps([1, 2, 3]).encode())
    with pytest.raises(SignatureError):
        await meta_signed_request.verify(f"{_sign(encoded)}.{encoded}", "meta-instagram")
