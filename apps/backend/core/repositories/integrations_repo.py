"""Per-owner, per-provider integration STATUS index.

Secret-free: holds only which providers an owner has connected and the
connection status. The credential itself lives in Secrets Manager
(backend-custodian providers — see ``integration_secret_store``) or the
container EFS keyring (actor-refresh providers). Backed by the ``integrations``
table (owner_id PK, provider SK)."""

from __future__ import annotations

import time

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread


def _table():
    return get_table("integrations")  # short name; table prefix auto-applied


async def put_status(owner_id: str, provider: str, status: str = "connected", identity: str | None = None) -> None:
    """Record that this owner has this provider connected.

    ``identity`` is the human-readable handle the materializer resolved for the
    account actually connected — an Instagram username, a Google email. It is NOT
    a credential and never a token; it exists so Settings can say WHICH account is
    wired up, which matters most where an owner could plausibly have several.
    Optional because it only lands on connects made after this shipped: older rows
    have none and the UI simply omits the line.
    """
    now = int(time.time())
    item: dict = {
        "owner_id": owner_id,
        "provider": provider,
        "status": status,
        "connected_at": now,
        "updated_at": now,
    }
    if identity:
        item["identity"] = identity
    await run_in_thread(_table().put_item, Item=item)


async def get(owner_id: str, provider: str) -> dict | None:
    response = await run_in_thread(
        _table().get_item,
        Key={"owner_id": owner_id, "provider": provider},
    )
    return response.get("Item")


async def list_for_owner(owner_id: str) -> list[dict]:
    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])


async def delete(owner_id: str, provider: str) -> None:
    await run_in_thread(
        _table().delete_item,
        Key={"owner_id": owner_id, "provider": provider},
    )


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Delete every integration row for this owner. Returns the providers.

    Composed from list + per-provider delete rather than a scan: the table is
    partitioned by owner_id, so the query is exact and cheap. Used by the
    account wipe, which previously left these rows behind — a "wiped" owner
    kept showing connected integrations.
    """
    rows = await list_for_owner(owner_id)
    providers = [r["provider"] for r in rows if r.get("provider")]
    for provider in providers:
        await delete(owner_id, provider)
    return providers


async def get_google_watch_channels(owner_id: str) -> list[dict]:
    """The owner's Gmail push watches (``kind="gmail_watch"`` entries) —
    stored as a list attribute on the existing ``(owner_id, "google")`` status
    row rather than a table of their own. The gmail trigger seed is the only
    writer and the trigger proxy's ``_gmail_watch_token`` the only reader:
    each entry binds a trigger (``trigger_public_id``) to its
    ``channel_token`` (= the trigger's webhook secret — stored because the
    proxy must inject it as the bearer after OIDC verification and Paperclip
    has no secret read-back; this table is CMK-encrypted and Paperclip
    already holds the identical value on the trigger row). Calendar/Drive
    push channels no longer live here — their state is the agent-side EFS
    file the seed writes, and the routine's own runs renew them (see
    ``inbound_triggers``)."""
    row = await get(owner_id, "google")
    return list((row or {}).get("google_watch_channels") or [])


async def set_google_watch_channels(owner_id: str, channels: list[dict]) -> None:
    """Overwrite the ``google_watch_channels`` list on the owner's "google"
    row. UpdateExpression rather than put_item so the row's status fields
    survive (and the row is created if somehow absent).

    Callers do read-modify-write of the list. The row is per-owner and the
    gmail trigger seed is the only writer, so the only contention is two
    concurrent seeds by the same owner — a lost update is tolerable: the
    orphaned watch's deliveries 401 at the proxy and the watch lapses at its
    ~7-day expiry."""
    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": "google"},
        UpdateExpression="SET google_watch_channels = :channels, updated_at = :now",
        ExpressionAttributeValues={":channels": channels, ":now": now},
    )


async def get_meta_webhook_bindings(owner_id: str, provider: str) -> list[dict]:
    """The owner's Meta asset↔trigger bindings for ``provider`` (``meta_ads``
    Pages / ``instagram`` accounts) — a list attribute on the existing
    ``(owner_id, provider)`` status row, same shape as the gmail watch list
    above rather than a table of its own.

    Each entry binds one Meta asset to one automation:
    ``{asset_id, asset_name, object_type, event_id, fields,
    trigger_public_id, trigger_secret}``. The secret is stored for the same
    reason the gmail entry's ``channel_token`` is: Meta signs deliveries with
    the APP secret, not a per-trigger one, so the app-level ingress
    (``routers/meta_webhook.py``) must inject the trigger's own bearer itself
    and Paperclip has no secret read-back. The trigger seed is the only
    writer; that route is the only reader."""
    row = await get(owner_id, provider)
    return list((row or {}).get("meta_webhook_bindings") or [])


async def set_meta_webhook_bindings(owner_id: str, provider: str, bindings: list[dict]) -> None:
    """Overwrite the ``meta_webhook_bindings`` list on the owner's ``provider``
    row. UpdateExpression (not put_item) so the row's status fields survive —
    identical posture, and identical lost-update tolerance, to
    ``set_google_watch_channels``."""
    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": provider},
        UpdateExpression="SET meta_webhook_bindings = :bindings, updated_at = :now",
        ExpressionAttributeValues={":bindings": bindings, ":now": now},
    )


async def get_aws_eventbridge_destinations(owner_id: str) -> list[dict]:
    """Secret-free lifecycle records for the owner's alarm destinations."""

    row = await get(owner_id, "aws")
    return list((row or {}).get("aws_eventbridge_destinations") or [])


async def set_aws_eventbridge_destinations(owner_id: str, destinations: list[dict]) -> None:
    """Persist managed resource names so disconnect can remove them first."""

    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": "aws"},
        UpdateExpression="SET aws_eventbridge_destinations = :destinations, updated_at = :now",
        ExpressionAttributeValues={":destinations": destinations, ":now": now},
    )


async def scan_all() -> list[dict]:
    """Every row in the table, paginated.

    Used by the orphan sweep — a full scan is the ONLY way to find rows whose
    owner no longer exists, because the owner id is gone from everywhere that
    would let you query for it — and by the Meta app-level webhook's
    asset→owner routing map, which faces the same shape of problem: the
    delivery names an asset id and nothing else, so there is no key to query
    by. That reader caches the map (see routers/meta_webhook.py) rather than
    scanning per request.
    """
    table = _table()
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last
