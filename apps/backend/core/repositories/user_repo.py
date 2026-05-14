"""User repository -- DynamoDB operations for the users table."""

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("users")


async def get(user_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"user_id": user_id})
    return response.get("Item")


# Alias for naming uniformity with the other per-user repos (which all
# expose ``get_by_owner_id`` / ``get_by_user_id`` accessors). Used by the
# debug ``/ddb-rows`` teardown-verification endpoint so the call surface
# is consistent across all 8 per-user tables.
get_by_user_id = get


async def put(user_id: str, email: str | None = None) -> dict:
    """Persist a users-table row.

    ``email`` is optional for back-compat with rows written before the
    column existed; the Paperclip provisioning path needs it (the org
    owner's email is required to sign them in to Better Auth) and the
    Clerk ``user.created`` webhook handler now always supplies it.
    Writing an empty string would shadow Clerk's actual primary email
    in any future upsert, so we omit the attribute entirely when the
    caller has nothing to record.
    """
    table = _get_table()
    item: dict = {"user_id": user_id, "created_at": utc_now_iso()}
    if email:
        item["email"] = email
    await run_in_thread(table.put_item, Item=item)
    return item


async def delete(user_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"user_id": user_id})


# ---------------------------------------------------------------------------
# Seller username (marketplace identity) helpers.
#
# A user's seller handle is the lowercased canonical form claimed at first
# publish. The atomic claim itself happens against the dedicated
# ``seller_usernames`` table (see ``core.services.seller_identity``); these
# helpers mirror that handle onto the users row so per-listing reads can
# look it up by ``user_id`` without a second DDB call indirection.
# ---------------------------------------------------------------------------


async def get_seller_username(user_id: str) -> dict | None:
    """Return ``{seller_username, seller_username_changed_at}`` if set, else None.

    Returns None for users who have never claimed a handle (or whose users
    row doesn't exist yet). Callers should treat None as "not yet claimed"
    and surface the publish-blocked or claim-required UX.
    """
    item = await get(user_id)
    if not item:
        return None
    username = item.get("seller_username")
    if not username:
        return None
    return {
        "seller_username": username,
        "seller_username_changed_at": item.get("seller_username_changed_at"),
    }


async def set_seller_username(
    user_id: str,
    username: str,
    last_changed_at: str,
) -> None:
    """Persist the claimed lowercased handle + change timestamp onto the
    users row.

    Called from ``seller_identity.claim_username`` after the atomic
    reservation row write succeeds in the ``seller_usernames`` table.
    The username MUST be the lowercased canonical form — never the raw
    user input. ``last_changed_at`` is an ISO-8601 UTC timestamp; the
    90-day cooldown is computed off of it.
    """
    table = _get_table()
    await run_in_thread(
        table.update_item,
        Key={"user_id": user_id},
        UpdateExpression=(
            "SET seller_username = :u, seller_username_changed_at = :t"
        ),
        ExpressionAttributeValues={
            ":u": username,
            ":t": last_changed_at,
        },
    )
