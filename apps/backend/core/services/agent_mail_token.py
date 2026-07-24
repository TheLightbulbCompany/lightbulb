"""Opaque, tamper-proof reply context for inbound email.

`/hooks/agent` has no metadata field, so the reply binding (who to answer,
which message to thread under) is inlined into the delivered message as an
opaque token. Encrypting it (Fernet) means the agent — and anyone who can
read the container's conversation — cannot forge a reply to an arbitrary
recipient: the outbound relay only honours a token IT can decrypt. Shared
with the outbound relay (Plan 3), which is the sole decoder.
"""

import json
from typing import TypedDict

from core.encryption import decrypt, encrypt  # use the real names from core/encryption.py


class ReplyContext(TypedDict):
    owner_id: str
    agent_id: str
    reply_to: str
    in_reply_to: str
    subject: str


def encode_reply_token(ctx: ReplyContext) -> str:
    return encrypt(json.dumps(ctx, separators=(",", ":")))


_REQUIRED_KEYS = ("owner_id", "agent_id", "reply_to", "in_reply_to", "subject")


def decode_reply_token(token: str) -> ReplyContext | None:
    if not token:
        return None
    try:
        payload = json.loads(decrypt(token))
    except Exception:  # noqa: BLE001 — any tamper/format error → not a valid token
        return None

    # `core/encryption.py`'s Fernet key is shared across the whole system, so
    # a DIFFERENT encrypted value (or an old-format token after a schema
    # change) can decrypt successfully and hand back a wrong-shape object —
    # a dict missing keys, a list, a bare string. Validate the shape before
    # trusting it as a ReplyContext; the outbound relay indexes into this
    # with ctx["reply_to"] etc. and a wrong-shape return would crash it.
    if not isinstance(payload, dict):
        return None
    for key in _REQUIRED_KEYS:
        if not isinstance(payload.get(key), str):
            return None
    return payload  # type: ignore[return-value]
