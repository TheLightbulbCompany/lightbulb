"""Verification for Meta's ``signed_request`` payload.

Meta POSTs this to the deauthorize and data-deletion callbacks a business app
must register. The format is ``<signature>.<payload>``, both base64url without
padding: ``payload`` is JSON, ``signature`` is HMAC-SHA256 of the *raw encoded
payload string* keyed by the app secret.

Two details are easy to get wrong and both are load-bearing:

- The HMAC covers the **encoded** payload, not the decoded JSON. Re-serialising
  the dict and signing that will not match — JSON key order and separators are
  not stable.
- ``algorithm`` inside the payload is attacker-supplied. It is checked against
  HMAC-SHA256 *after* the signature verifies, and is never used to select the
  algorithm — honouring it would be the classic "alg: none" downgrade.

Every failure raises :class:`SignatureError`. Callers treat that as 400 and
must not act on the payload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

from core.services.credentials import platform_oauth_app_store


class SignatureError(Exception):
    """The signed_request was absent, malformed, or did not verify."""


def _b64_decode(segment: str) -> bytes:
    # Meta strips base64url padding; restore it before decoding.
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("signed_request segment is not valid base64url") from exc


async def verify(signed_request: str, platform_app: str) -> dict:
    """Return the decoded payload, or raise :class:`SignatureError`.

    ``platform_app`` names the Isol8-owned OAuth app whose secret signs the
    request (``meta-instagram`` / ``meta-ads``) — the same name the provider's
    ``oauth.platform_app`` carries.
    """
    if not signed_request or not isinstance(signed_request, str):
        raise SignatureError("missing signed_request")

    parts = signed_request.split(".")
    if len(parts) != 2:
        raise SignatureError("signed_request must be '<signature>.<payload>'")
    encoded_sig, encoded_payload = parts
    if not encoded_sig or not encoded_payload:
        raise SignatureError("signed_request has an empty segment")

    app = await platform_oauth_app_store.get(platform_app)
    secret = (app or {}).get("client_secret")
    if not secret:
        # Fail closed. An unprovisioned app secret must never read as "valid" —
        # that would let anyone POST a teardown for any user.
        raise SignatureError(f"platform OAuth app '{platform_app}' is not provisioned")

    expected = hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("utf-8"),  # the ENCODED string, not the decoded JSON
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _b64_decode(encoded_sig)):
        raise SignatureError("signed_request signature mismatch")

    try:
        payload = json.loads(_b64_decode(encoded_payload))
    except ValueError as exc:
        raise SignatureError("signed_request payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise SignatureError("signed_request payload is not an object")

    # Checked only after the signature holds, and never used to pick the
    # algorithm — the payload is attacker-controlled until this point.
    algorithm = str(payload.get("algorithm") or "")
    if algorithm.upper().replace("-", "") != "HMACSHA256":
        raise SignatureError(f"unexpected signed_request algorithm: {algorithm!r}")

    return payload
