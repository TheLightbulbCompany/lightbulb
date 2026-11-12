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


async def put_status(
    owner_id: str,
    provider: str,
    status: str = "connected",
    identity: str | None = None,
    tenant_id: str | None = None,
    custody: str | None = None,
) -> None:
    """Record that this owner has this provider connected.

    ``identity`` is the human-readable handle the materializer resolved for the
    account actually connected — an Instagram username, a Google email. It is NOT
    a credential and never a token; it exists so Settings can say WHICH account is
    wired up, which matters most where an owner could plausibly have several.
    Optional because it only lands on connects made after this shipped: older rows
    have none and the UI simply omits the line.

    ``tenant_id`` is the Microsoft Entra tenant (``tid``) the grant is bound to —
    also metadata, never a credential.
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
    if tenant_id:
        item["tenant_id"] = tenant_id
    if custody:
        # Spec 13: who refreshes this row's rotating grant ("backend"); absent = today's rule.
        item["custody"] = custody
    await run_in_thread(_table().put_item, Item=item)


async def set_status(owner_id: str, provider: str, status: str, *, reason: str | None = None) -> None:
    """Flip an existing row's status (e.g. ``reconnect_required``), keeping its identity,
    tenant and connected_at -- unlike ``put_status``, which rewrites the whole row.

    ``reason`` (secret-free: the provider's OAuth error or our own short code) lands as
    ``reconnect_reason`` + ``reconnect_reason_at``. A reconnect clears both: ``put_status``
    rewrites the row and ``add_connected_team`` removes them."""
    now = int(time.time())
    expr = "SET #s = :s, updated_at = :u"
    values: dict = {":s": status, ":u": now}
    if reason:
        expr += ", reconnect_reason = :r, reconnect_reason_at = :u"
        values[":r"] = reason
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": provider},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )


async def set_custody(owner_id: str, provider: str, custody: str, *, expected: str | None = None) -> None:
    """Move an existing row to ``custody`` (cutover/rollback, spec 13 §6.2), only from
    ``expected`` (None = no custody attribute yet). Raises ``ConditionalCheckFailedException``
    when the row is gone or its custody moved: the caller re-reads."""
    condition = "attribute_exists(owner_id) AND " + (
        "attribute_not_exists(custody)" if expected is None else "custody = :expected"
    )
    values: dict = {":c": custody, ":u": int(time.time())}
    if expected is not None:
        values[":expected"] = expected
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": provider},
        UpdateExpression="SET custody = :c, updated_at = :u",
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )


async def get(owner_id: str, provider: str, *, consistent: bool = False) -> dict | None:
    response = await run_in_thread(
        _table().get_item,
        Key={"owner_id": owner_id, "provider": provider},
        ConsistentRead=consistent,
    )
    return response.get("Item")


async def list_for_owner(owner_id: str, *, consistent: bool = False) -> list[dict]:
    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        ConsistentRead=consistent,
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


# ---------------------------------------------------------------------------
# Provider resources provisioned through Stripe Projects.
#
# POINTERS ONLY (spec §2, §4.3): resource id, name, service, status, env var
# NAMES, dashboard URL. Never a value — credentials live in Secrets Manager
# (integration_secret_store), never in DynamoDB (spec §3.5).
#
# NAMESPACED under `projects/{provider}`: the bare sort key belongs to the
# OAuth integrations rail, and several providers (stripe, posthog, sentry,
# cloudflare) exist on BOTH. Sharing the key would make every OAuth reader
# (routers/connections.py, credentials/mcp_server.py, catalog_service.py)
# treat a provisioned provider as OAuth-connected, 409 the OAuth start, and —
# worst — let an OAuth Disconnect force-delete this rail's credentials.
# `integration_secret_store` is namespaced with the SAME key for the same
# reason: one blob per rail, never a shared one.
#
# UpdateExpression rather than put_status's put_item: put_status full-replaces
# the item, so routing these through it would drop identity /
# google_watch_channels / meta_webhook_bindings. Same reasoning as
# set_google_watch_channels above.
# ---------------------------------------------------------------------------

PROJECTS_ACTOR = "projects"
PROJECTS_KEY_PREFIX = "projects/"


def projects_key(provider: str) -> str:
    """The Stripe Projects rail's storage key for ``provider``.

    The ONE place the namespace is spelled. Used for both the ``integrations``
    sort key (here) and the Secrets Manager provider segment
    (``provisioning_landing``) — a `/` is legal in a Secrets Manager name and
    already used by ``oauth_app_store``'s ``oauth-app/{provider}``.
    """
    return f"{PROJECTS_KEY_PREFIX}{provider}"


async def list_provider_resources(owner_id: str, provider: str) -> list[dict]:
    row = await get(owner_id, projects_key(provider))
    return list((row or {}).get("provider_resources") or [])


async def _write_provider_resources(owner_id: str, provider: str, resources: list[dict]) -> list[dict]:
    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": projects_key(provider)},
        UpdateExpression=(
            "SET provider_resources = :r, actor = :a, #st = :s, updated_at = :now, "
            "connected_at = if_not_exists(connected_at, :now)"
        ),
        # `status` is a DynamoDB reserved word.
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":r": resources, ":a": PROJECTS_ACTOR, ":s": "connected", ":now": now},
    )
    return resources


async def record_provider_resource(owner_id: str, provider: str, resource: dict) -> list[dict]:
    """Upsert one provisioned resource pointer. Idempotent per ``resource_id``.

    Read-modify-write of a per-(owner, provider) list. The only writer is
    ``provisioning_workflow._bind``, and each workflow binds under its own
    driver lease, so a lost update needs two DIFFERENT workflows for the same
    provider to bind in the same instant. Accepted: the losing pointer is a
    credential still present in Secrets Manager and in the container's env, and
    the workflow row keeps its own ``resource_name``/``env_keys`` copy.
    """
    resource_id = resource.get("resource_id")
    if not resource_id:
        raise ValueError("resource must carry a resource_id")
    existing = await list_provider_resources(owner_id, provider)
    merged = [r for r in existing if r.get("resource_id") != resource_id] + [resource]
    merged.sort(key=lambda r: r.get("resource_id") or "")
    return await _write_provider_resources(owner_id, provider, merged)


async def remove_provider_resource(owner_id: str, provider: str, resource_id: str) -> list[dict]:
    """Drop OUR pointer to a resource. Does NOT touch the provider — the owner
    owns that resource and keeps it (spec §3.6)."""
    existing = await list_provider_resources(owner_id, provider)
    kept = [r for r in existing if r.get("resource_id") != resource_id]
    if len(kept) == len(existing):
        return existing
    return await _write_provider_resources(owner_id, provider, kept)


# ---------------------------------------------------------------------------
# Manually-defined MCP servers (an AgentCore owner's raw-editor entries).
#
# On ECS these go straight into openclaw.json's mcp.servers and never touch
# this table. An AgentCore owner has no openclaw.json to write into, so
# the retired manual-MCP CRUD stored them here (rows are still read) -- one reserved sort key,
# same pattern as `projects_key`: the bare key belongs to no registry
# provider, so it can never collide with a real one and the orphan-skip in
# mcp_server.build_connected_integration_servers's provider loop never sees
# it (it's read by a dedicated getter, not that loop).
# ---------------------------------------------------------------------------

MANUAL_MCP_KEY = "manual_mcp"


async def get_manual_mcp_servers(owner_id: str) -> dict:
    row = await get(owner_id, MANUAL_MCP_KEY)
    return dict((row or {}).get("servers") or {})


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
