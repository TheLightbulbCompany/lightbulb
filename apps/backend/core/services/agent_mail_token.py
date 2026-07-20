"""Opaque, tamper-proof reply context for inbound email.

`/hooks/agent` has no metadata field, so the reply binding (who to answer,
which message to thread under) is inlined into the delivered message as an
opaque token. Encrypting it (Fernet) means the agent — and anyone who can
read the container's conversation — cannot forge a reply to an arbitrary
recipient: the outbound relay only honours a token IT can decrypt. Shared
with the outbound relay (Plan 3), which is the sole decoder.
"""

import json
import logging
from typing import TypedDict

from core.encryption import decrypt, encrypt  # use the real names from core/encryption.py

logger = logging.getLogger(__name__)


class ReplyContext(TypedDict):
    owner_id: str
    agent_id: str
    reply_to: str
    in_reply_to: str
    subject: str


def encode_reply_token(ctx: ReplyContext) -> str:
    return encrypt(json.dumps(ctx, separators=(",", ":")))


def decode_reply_token(token: str) -> ReplyContext | None:
    if not token:
        return None
    try:
        return json.loads(decrypt(token))
    except Exception:  # noqa: BLE001 — any tamper/format error → not a valid token
        return None
