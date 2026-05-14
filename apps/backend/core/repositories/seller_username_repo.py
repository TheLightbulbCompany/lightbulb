"""Seller username reservation repository.

The ``seller_usernames`` table is keyed by ``username_lower`` (PK) and acts
as the platform-wide uniqueness oracle for marketplace seller handles.
Every claim goes through a conditional ``PutItem`` with
``attribute_not_exists(username_lower)``; a failed conditional check is
how we detect "this handle is already taken" without a separate read.

A GSI on ``owner_id`` enables reverse lookups — "what handle does this
owner currently hold?" — without a table scan. Used by
``seller_identity.get_for_owner`` (cooldown check) and by the catalog-read
enrichment that resolves ``seller_id -> @handle`` per listing card.

Row shape::

    {
        "username_lower": "ledger",      # PK — always lowercased
        "owner_id":       "org_xxx",     # GSI PK — the owner that claimed it
                                          # (Clerk user_id for personal owners,
                                          #  Clerk org_id for org owners)
        "claimed_at":     "<ISO-8601>",
    }

Owners may end up with multiple rows over time (handle changes leave the
old row dangling so it can't be re-claimed). The "current" handle for an
owner is the row with the latest ``claimed_at`` — see ``get_current_for_owner``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread


class UsernameTakenError(Exception):
    """The conditional ``PutItem`` failed because a row already exists for
    this lowercased username. The service layer surfaces this as a
    ``SellerUsernameError('taken')`` to the caller.
    """


# GSI name on the seller-usernames table — keyed by owner_id, sorted by
# claimed_at (so the latest row per owner is the partition's tail).
OWNER_ID_GSI = "by-owner-id"


def _get_table():
    return get_table("seller-usernames")


async def get(username_lower: str) -> dict | None:
    """Look up a seller-usernames row by lowercased handle.

    Used by admin tooling / debug surfaces; the happy claim path doesn't
    read first (the conditional write is its own check).
    """
    table = _get_table()
    response = await run_in_thread(
        table.get_item, Key={"username_lower": username_lower}
    )
    return response.get("Item")


async def claim(*, username_lower: str, owner_id: str, claimed_at: str) -> dict:
    """Atomically reserve ``username_lower`` for ``owner_id``.

    Conditional on ``attribute_not_exists(username_lower)``. Raises
    ``UsernameTakenError`` if another owner already holds the row.

    ``owner_id`` is the Clerk org_id for org owners, the Clerk user_id for
    personal owners — i.e., the value returned by
    ``core.auth.resolve_owner_id(auth)``. This mirrors how every other
    Isol8 surface (containers, billing) keys ownership.
    """
    table = _get_table()
    item = {
        "username_lower": username_lower,
        "owner_id": owner_id,
        "claimed_at": claimed_at,
    }
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(username_lower)",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise UsernameTakenError(username_lower) from exc
        raise
    return item


async def get_current_for_owner(owner_id: str) -> dict | None:
    """Return the current (latest) reservation row for ``owner_id``.

    Queries the ``by-owner-id`` GSI. An owner may have multiple rows
    (handle changes leave the old row dangling for re-claim protection);
    we return the one with the latest ``claimed_at``.

    Returns None when the owner has never claimed a handle.
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName=OWNER_ID_GSI,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        ScanIndexForward=False,  # newest first by claimed_at sort key
        Limit=1,
    )
    items = response.get("Items") or []
    return items[0] if items else None


async def release(username_lower: str) -> None:
    """Delete a reservation row -- used by admin / test teardown.

    Not exposed via the public API; the happy path is a single-claim model
    and changing handles writes a NEW reservation row (the previous handle
    is intentionally left dangling so it can't be re-claimed by someone
    else immediately).
    """
    table = _get_table()
    await run_in_thread(
        table.delete_item, Key={"username_lower": username_lower}
    )
