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
import time

import pytest
from fastapi import HTTPException

from core.config import settings
from routers.webhooks import _SVIX_TOLERANCE_S, _verify_svix_signature

_KEY = b"0123456789abcdef0123456789abcdef"
_SECRET = "whsec_" + base64.b64encode(_KEY).decode()
_BODY = b'{"type":"user.created","data":{"id":"u_1"}}'
_MSG_ID = "msg_1"


def _signed_headers(key: bytes = _KEY, body: bytes = _BODY, age_s: float = 0) -> dict:
    """Sign like svix does. The timestamp must be generated relative to NOW —
    a hardcoded one drifts out of the replay window and the tests rot."""
    ts = str(int(time.time() - age_s))
    signed = f"{_MSG_ID}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"svix-id": _MSG_ID, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


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


def test_replayed_old_payload_is_rejected(monkeypatch):
    """A captured (payload, signature) pair stays valid forever without a
    freshness check — and replaying a user.deleted is the exact destructive
    action the signature exists to prevent."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    stale = _signed_headers(age_s=_SVIX_TOLERANCE_S + 60)
    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, stale)

    assert exc.value.status_code == 400


def test_future_timestamp_beyond_tolerance_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, _signed_headers(age_s=-(_SVIX_TOLERANCE_S + 60)))

    assert exc.value.status_code == 400


def test_clock_skew_within_tolerance_still_accepted(monkeypatch):
    """Ordinary skew between Clerk and the task must NOT reject live traffic."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    _verify_svix_signature(_BODY, _signed_headers(age_s=_SVIX_TOLERANCE_S - 30))
    _verify_svix_signature(_BODY, _signed_headers(age_s=-(_SVIX_TOLERANCE_S - 30)))


def test_non_numeric_timestamp_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    headers = _signed_headers()
    headers["svix-timestamp"] = "not-a-number"
    with pytest.raises(HTTPException) as exc:
        _verify_svix_signature(_BODY, headers)

    assert exc.value.status_code == 400


def test_accepts_when_one_of_several_tokens_matches(monkeypatch):
    """svix-signature may carry several space-separated tokens during rotation."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "CLERK_WEBHOOK_SECRET", _SECRET)

    headers = _signed_headers()
    headers["svix-signature"] = f"v1,ZmFrZQ== {headers['svix-signature']}"

    _verify_svix_signature(_BODY, headers)  # must not raise
