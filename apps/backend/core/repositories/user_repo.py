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


# --- per-human preferences (daily digest, and any other local-time job) ---
DEFAULT_TIMEZONE = "America/New_York"


def valid_timezone(name: str | None) -> bool:
    """An IANA zone name ``zoneinfo`` can load (``Europe/Paris``, ``UTC``)."""
    from zoneinfo import ZoneInfo

    if not name or len(name) > 64:
        return False
    try:
        ZoneInfo(name)
    except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError; bad paths are ValueError
        return False
    return True


def timezone_of(row: dict | None) -> str:
    """The row's IANA ``timezone``, or ``DEFAULT_TIMEZONE`` when unset/unloadable."""
    tz = (row or {}).get("timezone")
    return tz if valid_timezone(tz) else DEFAULT_TIMEZONE


async def get_timezone(user_id: str) -> str:
    """The human's IANA timezone (``users.timezone``), default ``America/New_York``."""
    return timezone_of(await get(user_id))


def daily_summary_on(row: dict | None) -> bool:
    """The "Daily summary" pref (``users.daily_summary``). Absent means ON."""
    return (row or {}).get("daily_summary") is not False


def chat_theme_of(row: dict | None) -> str | None:
    """The account's chat theme (``users.chat_theme``), or None when never set
    on any device. Unlike ``timezone_of``/``daily_summary_on`` this has no
    coerced default: a client uses null to mean "defer to this device's own
    localStorage/OS preference" rather than overwriting it with a guess."""
    theme = (row or {}).get("chat_theme")
    return theme if theme in ("light", "dark") else None


def agent_send_without_asking(row: dict | None) -> bool:
    """``users.agent_send_without_asking``: agents message this person's sessions without an
    ask (spec 2026-10-07-agent-session-approval). Absent means OFF: they ask."""
    return (row or {}).get("agent_send_without_asking") is True


def sessions_reachable_from_background_work(row: dict | None) -> bool:
    """``users.sessions_reachable_from_background_work``: Paperclip runs, heartbeats and
    routines may reach this person's shared sessions. Absent means OFF."""
    return (row or {}).get("sessions_reachable_from_background_work") is True


async def set_prefs(
    user_id: str,
    *,
    timezone: str | None = None,
    daily_summary: bool | None = None,
    chat_theme: str | None = None,
    agent_send_without_asking: bool | None = None,
    sessions_reachable_from_background_work: bool | None = None,
) -> None:
    """Scoped SET of the given prefs on an existing row (no-op when the row is
    absent or nothing is given). Callers validate ``timezone``/``chat_theme``
    first."""
    values = {
        k: v
        for k, v in (
            ("timezone", timezone),
            ("daily_summary", daily_summary),
            ("chat_theme", chat_theme),
            ("agent_send_without_asking", agent_send_without_asking),
            ("sessions_reachable_from_background_work", sessions_reachable_from_background_work),
        )
        if v is not None
    }
    if not values:
        return
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={f"#{k}": k for k in values},
            ExpressionAttributeValues={f":{k}": v for k, v in values.items()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
