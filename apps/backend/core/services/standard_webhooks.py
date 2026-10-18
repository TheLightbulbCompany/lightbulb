"""Standard Webhooks (Svix-style) signature check, shared by every sender that uses it (Photon, Recall)."""

import base64
import binascii
import hashlib
import hmac
import time


def verify(headers, body: bytes, secret: str, *, tolerance_s: int = 300) -> str | None:
    """None when the delivery is authentic and fresh, else why not: ``missing``, ``stale`` or ``bad_signature``.

    ``v1,<b64 HMAC-SHA256(id.timestamp.body)>`` under the ``whsec_`` base64 secret; any of the space-separated
    signatures may match (key rotation). Verify over the RAW body, before anything parses it.
    """
    msg_id, stamp, sigs = (headers.get(f"webhook-{k}") for k in ("id", "timestamp", "signature"))
    if not (secret and msg_id and stamp and sigs):
        return "missing"
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
        sent_at = int(stamp)
    except (binascii.Error, ValueError):
        return "bad_signature"
    if abs(time.time() - sent_at) > tolerance_s:
        return "stale"
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest())
    for entry in sigs.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected):
            return None
    return "bad_signature"
