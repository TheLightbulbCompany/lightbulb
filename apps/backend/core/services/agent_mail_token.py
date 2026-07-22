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

from core.config import settings
from core.encryption import decrypt, encrypt  # use the real names from core/encryption.py
from core.repositories import agent_email_repo

logger = logging.getLogger(__name__)


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


async def mint_first_contact_token(*, owner_id: str, owner_email: str, subject: str) -> str | None:
    """A one-recipient token that lets Friday send ONE proactive first email to
    the owner. Returns None when agent email is inactive (AGENT_EMAIL_DOMAIN
    unset) or the owner has no active agent-email address yet — so this is
    inert-safe. The recipient is baked in (owner_email) and the agent is the
    owner's active Friday address row, so the token cannot email anyone else.

    Never raises: any lookup/encoding failure is logged and folds into a
    None return, same contract as ``send_reply`` -- a first-email mint must
    never take down the onboarding brief request that triggers it.
    """
    try:
        if not settings.AGENT_EMAIL_DOMAIN.strip():
            return None
        rows = await agent_email_repo.query_by_owner(owner_id)
        active = next((row for row in rows if row.get("status") == "active"), None)
        if active is None:
            return None
        agent_id = active["agent_id"]
        return encode_reply_token(
            {
                "owner_id": owner_id,
                "agent_id": agent_id,
                "reply_to": owner_email,
                "in_reply_to": "",
                "subject": subject,
            }
        )
    except Exception:
        logger.exception("mint_first_contact_token failed for owner_id=%s", owner_id)
        return None
