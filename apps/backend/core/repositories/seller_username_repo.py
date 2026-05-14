"""Seller username reservation repository.

The ``seller_usernames`` table is keyed by ``username_lower`` (PK) and acts
as the platform-wide uniqueness oracle for marketplace seller handles.
Every claim goes through a conditional ``PutItem`` with
``attribute_not_exists(username_lower)``; a failed conditional check is
how we detect "this handle is already taken" without a separate read.

Row shape::

    {
        "username_lower": "ledger",     # PK -- always lowercased
        "user_id":        "user_xxx",   # the claimant's Clerk user_id
        "claimed_at":     "<ISO-8601>",
    }
"""

from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread


class UsernameTakenError(Exception):
    """The conditional ``PutItem`` failed because a row already exists for
    this lowercased username. The service layer surfaces this as a
    ``SellerUsernameError('taken')`` to the caller.
    """


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


async def claim(*, username_lower: str, user_id: str, claimed_at: str) -> dict:
    """Atomically reserve ``username_lower`` for ``user_id``.

    Conditional on ``attribute_not_exists(username_lower)``. Raises
    ``UsernameTakenError`` if another user already owns the row.
    """
    table = _get_table()
    item = {
        "username_lower": username_lower,
        "user_id": user_id,
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
