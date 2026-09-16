"""Per-workspace join-link rows — exactly ONE row per Clerk org, keyed on org_id.

The row stores the SHA-256 of the join token (what redeem compares against)
and a Fernet ciphertext of it under the platform ``ENCRYPTION_KEY`` (what lets
an admin copy the SAME link again instead of regenerating). Never the plaintext:
a leaked table row is not a usable link without the key.

**Why keyed on org_id and not on the token hash.** The obvious shape is a
token-hash PK (redeem is then a single GetItem) plus a second pointer row so an
admin can see/replace the current link. That shape has a regenerate race: two
admins who both read pointer ``h0`` each delete ``h0`` and write their own new
row, and whichever loses the pointer write leaves a LIVE orphan token row that
nothing can ever find again — an un-revokable credential. Closing it needs a
conditional TransactWriteItems.

Keying on org_id instead removes the race by construction: the org's row IS the
current link, so regenerate is one PutItem (last writer wins, no orphan
possible) and revoke is one DeleteItem. The cost is that redeem must be told
which org to look up, which is why the join URL carries the org id in the path
and the secret in its fragment (``/join/{org_id}#{token}``) — an opaque Clerk id
that grants nothing on its own, exactly as Slack's and Discord's invite links
carry a workspace id.

No TTL attribute: the owner's decision is that join links do not expire. They
end when an admin regenerates or revokes.
"""

from __future__ import annotations

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_TABLE = "workspace-join-links"


def _table():
    return get_table(_TABLE)


async def get_link(org_id: str) -> dict | None:
    """The org's current join-link row, or None when it has never had one."""
    resp = await run_in_thread(_table().get_item, Key={"org_id": org_id})
    return resp.get("Item")


async def put_link(org_id: str, *, token_hash: str, token_enc: str, created_by: str) -> dict:
    """Install ``token_hash`` as the org's one live join link.

    A whole-item put, deliberately: it is what makes "regenerating invalidates
    the previous link" atomic and total. Returns the row it wrote so the caller
    can echo ``created_at`` without a read-back.
    """
    item = {
        "org_id": org_id,
        "token_hash": token_hash,
        "token_enc": token_enc,
        "created_by": created_by,
        "created_at": utc_now_iso(),
    }
    await run_in_thread(_table().put_item, Item=item)
    return item


async def delete_link(org_id: str) -> None:
    """Revoke: drop the row. Idempotent — DeleteItem on a missing key is a no-op."""
    await run_in_thread(_table().delete_item, Key={"org_id": org_id})
