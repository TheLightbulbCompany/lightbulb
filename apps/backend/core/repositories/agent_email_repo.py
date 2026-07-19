"""Address-book for provisioned agent email.

One row per provisioned agent email address. The address is the partition
key because inbound mail is routed by recipient; a ``by-owner`` GSI lists an
owner's addresses.

``status`` is ``"active"`` for the current primary address and ``"alias"``
for a superseded one that must keep routing — so an owner can accumulate
several rows over time and readers must select deliberately.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("agent-email-addresses")


def normalize(value: str) -> str:
    """Addresses and sender emails are matched case-insensitively."""
    return value.strip().lower()


async def put(
    *,
    address: str,
    owner_id: str,
    agent_id: str,
    verified_senders: list[str],
    public: bool = False,
) -> dict:
    item = {
        "address": normalize(address),
        "owner_id": owner_id,
        "agent_id": agent_id,
        "verified_senders": [normalize(s) for s in verified_senders],
        "public": public,
        "status": "active",
        "created_at": utc_now_iso(),
    }
    table = _get_table()
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_by_address(address: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"address": normalize(address)})
    return response.get("Item")


async def query_by_owner(owner_id: str) -> list[dict]:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])
