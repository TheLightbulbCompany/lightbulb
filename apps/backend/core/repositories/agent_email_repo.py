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
from botocore.exceptions import ClientError

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
) -> dict | None:
    """Claim ``address`` for ``owner_id`` — a CREATE primitive, not an update.

    This is a conditional put mirroring ``company_slug_repo.reserve``:
    ``attribute_not_exists(address) OR owner_id = :owner`` — so a concurrent
    or reordered write can never overwrite another owner's row. Returns the
    written item, or ``None`` when another owner already holds ``address``
    (on ``ConditionalCheckFailedException`` — never raises for that case).

    It ALWAYS writes ``status="active"`` and a fresh ``created_at``, even
    when re-affirming a row this owner already holds. There is deliberately
    no update primitive here: a future rename/alias flow that needs to demote
    a row to ``status="alias"`` without resetting ``created_at`` (the
    deterministic tie-break `owner_facts._lookup_email_address` sorts on)
    must add a dedicated update primitive rather than calling this.
    """
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
    try:
        await run_in_thread(
            table.put_item,
            Item=item,
            ConditionExpression="attribute_not_exists(address) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def get_by_address(address: str) -> dict | None:
    """Strongly consistent — this is the mint-time idempotency guard
    (`agent_email.mint_agent_email_address` calls it before `put`). An
    eventually-consistent miss right after a same-address write would let
    `put` "win" a race it should have folded into, overwriting the existing
    row and resetting its `created_at` — which flips the deterministic
    earliest-`created_at` tie-break `owner_facts._lookup_email_address`
    sorts on. Base-table GetItem supports `ConsistentRead`; the `by-owner`
    GSI in `query_by_owner` below does not, so that one stays eventual."""
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"address": normalize(address)}, ConsistentRead=True)
    return response.get("Item")


async def query_by_owner(owner_id: str) -> list[dict]:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])
