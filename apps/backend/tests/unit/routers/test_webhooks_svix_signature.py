"""_verify_svix_signature: the ONLY authentication on /webhooks/clerk.

Clerk has no bearer token to present, so the svix signature is the whole
security boundary. These tests pin the fail-CLOSED behaviour: a deployed env
with a missing or malformed secret must reject, not wave traffic through. The
regression they guard against is real — prod ran with no secret configured at
all, so every forged POST was accepted, and a forged `user.deleted` sweeps
channel_link rows and disables Paperclip for an arbitrary user id.
"""

import base64
import hashlib
import hmac

import pytest
from fastapi import HTTPException

from core.config import settings
from routers.webhooks import _verify_svix_signature

_KEY = b"0123456789abcdef0123456789abcdef"
_SECRET = "whsec_" + base64.b64encode(_KEY).decode()
_BODY = b'{"type":"user.created","data":{"id":"u_1"}}'
_MSG_ID = "msg_1"
_TS = "1700000000"


def _signed_headers(key: bytes = _KEY, body: bytes = _BODY) -> dict:
    signed = f"{_MSG_ID}.{_TS}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"svix-id": _MSG_ID, "svix-timestamp": _TS, "svix-signature": f"v1,{sig}"}


def test_deployed_env_with_no_secret_rejects(monkeypatch):
    """The exact prod misconfiguration: no secret set -> reject, don't accept."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", None)

    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, _signed_headers())

    # 503 (not 400) so svix retries and redelivers once the secret is fixed.
    assert exc.value.status_code == 503


def test_deployed_env_with_malformed_secret_rejects(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", "whsec_!!!not-base64!!!")

    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, _signed_headers())

    assert exc.value.status_code == 503


def test_local_dev_with_no_secret_still_skips(monkeypatch):
    """ENVIRONMENT unset = local dev, where no Clerk instance signs anything."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", None)

    _verify_svix_signature(_BODY, {})  # must not raise


def test_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    _verify_svix_signature(_BODY, _signed_headers())  # must not raise


def test_forged_signature_is_rejected(monkeypatch):
    """A signature minted with the wrong key must not be accepted."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, _signed_headers(key=b"a" * 32))

    assert exc.value.status_code == 400


def test_tampered_body_is_rejected(monkeypatch):
    """Signature is over the body — mutating the payload must invalidate it."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    headers = _signed_headers()
    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(b'{"type":"user.deleted","data":{"id":"u_victim"}}', headers)

    assert exc.value.status_code == 400


def test_missing_svix_headers_rejected(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, {})

    assert exc.value.status_code == 400


def test_accepts_when_one_of_several_tokens_matches(monkeypatch):
    """svix-signature may carry several space-separated tokens during rotation."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    headers = _signed_headers()
    headers["svix-signature"] = f"v1,ZmFrZQ== {headers['svix-signature']}"

    _verify_svix_signature(_BODY, headers)  # must not raise
