"""One row per app, keyed by ``app_id``.

Each app gets its OWN Stripe Project (``stripe_project_id``), so its
credentials never mix with the company's or another app's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from core.dynamodb import ddb_safe, get_table, run_in_thread, utc_now_iso

_PROTECTED_FIELDS = frozenset({"app_id", "owner_id", "created_at"})


class NameTaken(Exception):
    """App names are unique across all owners."""


def _guard_id(name: str) -> str:
    # A second item in the same table. It has no owner_id, so the owner-index
    # never contains it.
    return f"NAME#{name}"


def _table():
    return get_table("apps")


async def get(app_id: str, *, consistent_read: bool = False) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"app_id": app_id}, ConsistentRead=consistent_read)
    return resp.get("Item")


async def patch(app_id: str, fields: dict) -> dict:
    sets = ["updated_at = :now"]
    names: dict[str, str] = {}
    values: dict = {":now": utc_now_iso()}
    for i, (key, value) in enumerate(sorted(fields.items())):
        if key in _PROTECTED_FIELDS:
            raise ValueError(f"{key} cannot be set through patch")
        sets.append(f"#f{i} = :f{i}")
        names[f"#f{i}"] = key
        values[f":f{i}"] = ddb_safe(value)
    kwargs = {"ExpressionAttributeNames": names} if names else {}
    resp = await run_in_thread(
        _table().update_item,
        Key={"app_id": app_id},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression="attribute_exists(app_id)",
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
        **kwargs,
    )
    return resp.get("Attributes")


async def take_lease(app_id: str, *, seconds: int) -> bool:
    """Hold ``progress_lease_until`` for ``seconds`` unless another holder's is still running."""
    now = datetime.now(UTC)
    try:
        await run_in_thread(
            _table().update_item,
            Key={"app_id": app_id},
            UpdateExpression="SET progress_lease_until = :until",
            ConditionExpression="attribute_exists(app_id) AND (attribute_not_exists(progress_lease_until) OR "
            "attribute_type(progress_lease_until, :null_t) OR progress_lease_until < :now)",
            ExpressionAttributeValues={
                ":until": (now + timedelta(seconds=seconds)).isoformat(),
                ":now": now.isoformat(),
                ":null_t": "NULL",
            },
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def set_stripe_project_id(app_id: str, project_id: str) -> bool:
    """Record the app's Project, exactly once. ``False`` = no app row, or
    another worker stored first; the caller re-reads and uses the winner's."""
    if not project_id:
        raise ValueError("project_id must be non-empty")
    try:
        await run_in_thread(
            _table().update_item,
            Key={"app_id": app_id},
            UpdateExpression="SET stripe_project_id = :p, updated_at = :now",
            ConditionExpression="attribute_exists(app_id) AND "
            "(attribute_not_exists(stripe_project_id) OR attribute_type(stripe_project_id, :null_t))",
            ExpressionAttributeValues={":p": project_id, ":now": utc_now_iso(), ":null_t": "NULL"},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def create(
    *,
    owner_id: str,
    name: str,
    room_id: str,
    created_by: str | None,
    actor_id: str,
    actor_kind: str,
    origin: dict,
    services: list[str],
    open_to: str,
    budget_usd: int,
    display_name: str | None = None,
) -> dict:
    now = utc_now_iso()
    item = ddb_safe(
        {
            "app_id": "app_" + uuid.uuid4().hex,
            "owner_id": owner_id,
            "name": name,
            "display_name": display_name,
            "room_id": room_id,
            "created_by": created_by,
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "origin": origin,
            "services": services,
            "open_to": open_to,
            "budget_usd": budget_usd,
            "state": "building",
            "stripe_project_id": None,
            "vercel": None,
            "neon": None,
            "domain": None,
            "versions": [],
            "published_migrations": [],
            "live_version": None,
            "preview_url": None,
            "live_url": None,
            "last_opened_at": None,
            "destroy_requested_by": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    table = _table()
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {
                    "Put": {
                        "TableName": table.table_name,
                        "Item": {"app_id": _guard_id(name), "claimed_by": owner_id},
                        "ConditionExpression": "attribute_not_exists(app_id)",
                    }
                },
                {"Put": {"TableName": table.table_name, "Item": item}},
            ],
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons") or []
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                raise NameTaken(name) from e
        raise
    return item


async def _query_owner(owner_id: str, name: str | None = None) -> list[dict]:
    from boto3.dynamodb.conditions import Key

    cond = Key("owner_id").eq(owner_id)
    if name is not None:
        cond = cond & Key("name").eq(name)
    items: list[dict] = []
    kwargs: dict = {"IndexName": "owner-index", "KeyConditionExpression": cond}
    while True:
        resp = await run_in_thread(_table().query, **kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def get_by_name(owner_id: str, name: str) -> dict | None:
    items = await _query_owner(owner_id, name)
    return items[0] if items else None


async def list_for_owner(owner_id: str) -> list[dict]:
    return await _query_owner(owner_id)


async def list_pending(*, limit: int = 25) -> list[dict]:
    """Apps with a preview or a publish in flight, for the sweeper. A filtered scan: the table is
    small, and a cleared pending map is stored as NULL, which cannot key a sparse index."""
    from boto3.dynamodb.conditions import Attr

    kwargs: dict = {
        "FilterExpression": Attr("preview_pending").attribute_type("M") | Attr("publish_pending").attribute_type("M")
    }
    items: list[dict] = []
    while True:
        resp = await run_in_thread(_table().scan, **kwargs)
        items.extend(resp.get("Items", []))
        if len(items) >= limit or not resp.get("LastEvaluatedKey"):
            return items[:limit]
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def add_version(app_id: str, version: dict) -> dict:
    """Append atomically, so two concurrent builds both land."""
    sets = ["versions = list_append(if_not_exists(versions, :empty), :v)", "updated_at = :now"]
    values: dict = {":empty": [], ":v": [ddb_safe(version)], ":now": utc_now_iso()}
    if version.get("live"):
        sets.append("live_version = :n")
        values[":n"] = version["n"]
    resp = await run_in_thread(
        _table().update_item,
        Key={"app_id": app_id},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression="attribute_exists(app_id)",
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return resp["Attributes"]


async def record_release(
    app_id: str, version: dict, *, pending_deployment_id: str, known_versions: int, fields: dict
) -> dict | None:
    """Record a READY production deployment as version ``known_versions + 1``, live, in ONE write that
    also drops ``publish_pending``. Only while that deployment is still pending and no other version
    landed since the caller read the record; ``None`` = another reader won (re-read)."""
    n = known_versions + 1
    sets = ["versions = list_append(versions, :v)", "live_version = :n", "updated_at = :now"]
    names = {"#pp": "publish_pending", "#dep": "deployment_id"}
    values: dict = {
        ":v": [ddb_safe({**version, "n": n})],
        ":n": n,
        ":now": utc_now_iso(),
        ":dep": pending_deployment_id,
        ":len": known_versions,
    }
    for i, (key, value) in enumerate(sorted(fields.items())):
        if key in _PROTECTED_FIELDS or key in ("versions", "live_version", "publish_pending"):
            raise ValueError(f"{key} cannot be set here")
        sets.append(f"#f{i} = :f{i}")
        names[f"#f{i}"] = key
        values[f":f{i}"] = ddb_safe(value)
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"app_id": app_id},
            UpdateExpression="SET " + ", ".join(sets) + " REMOVE #pp",
            ConditionExpression="#pp.#dep = :dep AND size(versions) = :len",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp["Attributes"]


async def delete(app_id: str) -> None:
    """Remove the app and free its name together."""
    app = await get(app_id, consistent_read=True)
    if not app:
        return
    table = _table()
    await run_in_thread(
        table.meta.client.transact_write_items,
        TransactItems=[
            {"Delete": {"TableName": table.table_name, "Key": {"app_id": app_id}}},
            {"Delete": {"TableName": table.table_name, "Key": {"app_id": _guard_id(app["name"])}}},
        ],
    )


# --- invites: one item per link, keyed by the token's sha256 (the token itself is never stored) ---
# The owner sits under ``invite_owner_id`` and there is no ``name``, so neither GSI holds an invite.


async def put_invite(
    token_hash: str, *, target_app_id: str, invite_owner_id: str, email: str, expires_at: str, created_by: str
) -> None:
    await run_in_thread(
        _table().put_item,
        Item={
            "app_id": f"INVITE#{token_hash}",
            "kind": "invite",
            "target_app_id": target_app_id,
            "invite_owner_id": invite_owner_id,
            "email": email,
            "expires_at": expires_at,
            "used_at": None,
            "created_by": created_by,
            "created_at": utc_now_iso(),
        },
    )


async def claim_invite(token_hash: str, now: str) -> dict | None:
    """Mark the invite used, in one conditional write: ``None`` when it was never issued, is used
    (even by a concurrent claim) or expired at ``now`` (ISO, same format as ``expires_at``)."""
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"app_id": f"INVITE#{token_hash}"},
            UpdateExpression="SET used_at = :now",
            ConditionExpression="#k = :invite AND expires_at > :now AND "
            "(attribute_not_exists(used_at) OR attribute_type(used_at, :null_t))",
            ExpressionAttributeNames={"#k": "kind"},
            ExpressionAttributeValues={":now": now, ":invite": "invite", ":null_t": "NULL"},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp["Attributes"]
