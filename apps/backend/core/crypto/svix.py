"""Reusable Svix webhook-signature verification.

Extracted from the Clerk webhook router's inline verifier
(``routers/webhooks.py:_verify_svix_signature``) so other Svix-signed
webhooks (e.g. the inbound-email webhook) can reuse the exact same
HMAC-SHA256 scheme instead of hand-rolling a second copy.

Verification scheme: https://docs.svix.com/receiving/verifying-payloads/how

This module is a PURE function — it has no opinion on what to do when a
secret is unset (that policy differs per caller: the Clerk router skips
verification when ``CLERK_WEBHOOK_SECRET`` is empty, while callers with a
stricter posture may want to fail closed instead). Given a secret, body,
and headers, it answers exactly one question: is the signature valid?
"""

import base64
import hashlib
import hmac


def verify_svix_signature(*, secret: str, body: bytes, headers: dict) -> bool:
    """Return True iff the Svix signature headers validate ``body`` under ``secret``.

    Svix signs the payload with HMAC-SHA256 using a timestamp + body
    combination. The ``svix-signature`` header contains one or more
    ``v1,<base64>`` tokens; the payload is valid if ANY token matches.

    Returns False (never raises) on any missing header, malformed secret,
    or signature mismatch. Header lookup expects a plain dict with
    lowercase keys (Starlette's ``dict(request.headers)`` already
    lowercases them).
    """
    # Strip the ``whsec_`` prefix that Clerk/svix adds to the secret.
    raw_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw_secret)
    except Exception:
        # Deliberate fail-closed divergence from the Clerk inline version
        # (routers/webhooks.py:72-76), which logs and skips (fail-open) on a
        # malformed secret. Do not "fix" this back to match — it's intentional.
        return False

    msg_id = headers.get("svix-id", "")
    msg_timestamp = headers.get("svix-timestamp", "")
    msg_signature = headers.get("svix-signature", "")

    if not msg_id or not msg_timestamp or not msg_signature:
        return False

    try:
        signed_content = f"{msg_id}.{msg_timestamp}.".encode() + body
        expected = hmac.new(key, signed_content, hashlib.sha256).digest()
        expected_b64 = base64.b64encode(expected).decode()
    except Exception:
        return False

    # svix-signature may contain multiple space-separated tokens.
    tokens = msg_signature.split(" ")
    for token in tokens:
        if token.startswith("v1,"):
            candidate = token[3:]
            if hmac.compare_digest(expected_b64.encode("utf-8"), candidate.encode("utf-8")):
                return True

    return False
