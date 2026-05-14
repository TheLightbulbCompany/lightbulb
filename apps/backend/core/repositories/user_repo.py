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


# Seller-username helpers were previously stored as a mirror column on the
# users row. They've been removed in favour of an owner-scoped model — the
# seller-usernames table itself is the single source of truth, indexed by
# ``owner_id`` via the ``by-owner-id`` GSI so an org owner (no users-row)
# can still own a handle. See ``core/services/seller_identity.py`` and
# ``core/repositories/seller_username_repo.py``.
