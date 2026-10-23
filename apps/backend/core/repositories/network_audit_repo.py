"""lightbulb-{env}-network-audit: one row per minted proof (and, later, per relayed decision).

``pk`` is ``ISSUER`` for proofs or ``OWNER#<owner>`` for a workspace's decisions; ``sk`` is
``<ts>#<kind>#<id>``. The row expires (``expiresAt`` TTL) 30 days past the item it records.
The table is CDK's; this module never creates it.
"""

from __future__ import annotations

import secrets

from core.config import settings
from core.dynamodb import _get_resource, run_in_thread

RETAIN_S = 30 * 86_400


async def put(kind: str, fields: dict, *, at: int, item_expires_at: int, pk: str = "ISSUER") -> None:
    if not settings.NETWORK_AUDIT_TABLE:
        raise RuntimeError("NETWORK_AUDIT_TABLE is not configured")
    item = {
        "pk": pk,
        "sk": f"{at}#{kind}#{secrets.token_hex(8)}",
        "kind": kind,
        **fields,
        "expiresAt": item_expires_at + RETAIN_S,
    }
    await run_in_thread(_get_resource().Table(settings.NETWORK_AUDIT_TABLE).put_item, Item=item)
