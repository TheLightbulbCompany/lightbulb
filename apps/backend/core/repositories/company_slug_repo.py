"""Platform-unique company slugs.

One row per reserved slug, with the slug as the partition key — which makes
reservation a *conditional put*, the only atomic "claim this name" primitive
DynamoDB offers. Read-then-write would let two owners race onto the same
slug. A ``by-owner`` GSI answers the reverse question.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("company-slugs")


def normalize(value: str) -> str:
    return value.strip().lower()


async def reserve(*, slug: str, owner_id: str) -> bool:
    """Claim ``slug`` for ``owner_id``.

    Returns True when the slug is now held by this owner — including the
    case where they already held it, since provisioning is idempotent and
    may re-run. Returns False when another owner holds it.
    """
    table = _get_table()
    item = {
        "slug": normalize(slug),
        "owner_id": owner_id,
        "created_at": utc_now_iso(),
    }
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(slug) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def get_by_slug(slug: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"slug": normalize(slug)})
    return response.get("Item")


async def get_by_owner(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None
