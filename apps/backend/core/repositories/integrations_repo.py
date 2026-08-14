"""Per-owner, per-provider integration STATUS index.

Secret-free: holds only which providers an owner has connected and the
connection status. The credential itself lives in Secrets Manager
(backend-custodian providers — see ``integration_secret_store``) or the
container EFS keyring (actor-refresh providers). Backed by the ``integrations``
table (owner_id PK, provider SK)."""

from __future__ import annotations

import time

from boto3.dynamodb.conditions import Attr, Key

from core.dynamodb import get_table, run_in_thread


def _table():
    return get_table("integrations")  # short name; table prefix auto-applied


async def put_status(
    owner_id: str,
    provider: str,
    status: str = "connected",
    provider_user_id: str | None = None,
) -> None:
    now = int(time.time())
    item = {
        "owner_id": owner_id,
        "provider": provider,
        "status": status,
        "connected_at": now,
        "updated_at": now,
    }
    # The provider's own account id for this owner (Meta's app-scoped user id for
    # instagram/meta_ads). Not a secret — it is opaque, per-app, and useless
    # without the token — but it is the ONLY key Meta's deauthorize and
    # data-deletion callbacks send us, so without it here a teardown request
    # cannot be resolved to an owner at all. The credential carries the same
    # value, but that lives per-owner in Secrets Manager and is not searchable.
    if provider_user_id:
        item["provider_user_id"] = str(provider_user_id)
    await run_in_thread(_table().put_item, Item=item)


async def find_by_provider_user_id(provider: str, provider_user_id: str) -> dict | None:
    """The owner who connected `provider` as `provider_user_id`, or None.

    A table scan, deliberately: this runs only on Meta's deauthorize /
    data-deletion callbacks — single-digit events against a table with one row
    per owner-provider pair. A GSI would be real infrastructure (CDK change,
    deploy, backfill) bought for a lookup that runs a handful of times a year.
    Revisit if Meta connections reach the thousands.
    """
    want = str(provider_user_id or "")
    # An empty needle must never match. Rows connected before this field was
    # captured have no `provider_user_id`, and a falsy-coalescing compare would
    # match every one of them — turning a blank id in a callback into the
    # teardown of an arbitrary owner's connection.
    if not want:
        return None
    kwargs: dict = {
        "FilterExpression": Attr("provider").eq(provider) & Attr("provider_user_id").eq(want),
        "ProjectionExpression": "owner_id, provider, provider_user_id",
    }
    while True:
        response = await run_in_thread(_table().scan, **kwargs)
        for item in response.get("Items", []):
            if str(item.get("provider_user_id") or "") == want:
                return item
        last = response.get("LastEvaluatedKey")
        if not last:
            return None
        kwargs["ExclusiveStartKey"] = last


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
