"""User repository -- DynamoDB operations for the users table."""

from botocore.exceptions import ClientError

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


async def backfill_email(user_id: str, email: str | None) -> None:
    """Write ``email`` onto an existing row that has none. No-op otherwise.

    A targeted UpdateItem, NOT ``put`` — ``put`` is a whole-item overwrite and
    would drop ``push_tokens`` and anything else already on the row.

    Exists because the users-table email had exactly one writer, the Clerk
    ``user.created`` webhook, and that webhook is the only source
    ``launch_referral._email_for_owner`` can read (it runs inside a Stripe
    webhook, so there is no JWT to fall back to). When the webhook was left
    pointing at the retired domain through the rebrand, every row created in
    that window came out email-less and referral counting went silently dead.
    /users/sync now heals those rows from the caller's own JWT claim.
    """
    if not email:
        return
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET email = :e",
            ConditionExpression="attribute_exists(user_id) AND attribute_not_exists(email)",
            ExpressionAttributeValues={":e": email},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def delete(user_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"user_id": user_id})


async def set_push_token(user_id: str, device_id: str, entry: dict) -> None:
    """Write one Expo push token into the user's ``push_tokens`` map.

    Scoped, server-atomic UpdateItem touching only its one map key (mirrors
    container_repo._put_paired_device) so a concurrent write of another device
    or another attribute can't clobber it. ``entry`` = {token, platform, updatedAt}.
    Requires the users row to exist (it always does for a signed-in user who
    hit /users/sync); no-op if it doesn't.
    """
    table = _get_table()
    # 1. Ensure the map exists — a nested SET fails if its parent map is absent.
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET #pt = if_not_exists(#pt, :empty)",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no users row — nothing to write onto
        raise
    # 2. Scoped set of the single device key.
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET #pt.#did = :entry",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens", "#did": device_id},
            ExpressionAttributeValues={":entry": entry},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # row deleted mid-write — no-op
        raise


async def clear_push_token(user_id: str, device_id: str) -> None:
    """Remove one device's push token (sign-out, or Expo DeviceNotRegistered prune).

    Scoped REMOVE of the single map key so a racing writer of other attributes
    cannot resurrect the removed entry. No-op if the row is absent.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="REMOVE #pt.#did",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#pt": "push_tokens", "#did": device_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no users row — nothing to remove
        raise


async def list_all() -> list[dict]:
    """Every users row, paginated. Used by the orphan sweep as the liveness
    signal: wipe_account deletes this row, so an owner id that appears in a
    credential table but not here has no account left."""
    table = _get_table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
