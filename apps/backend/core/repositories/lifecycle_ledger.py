"""Atomic lifecycle-send ledger backed by reserved rows in the containers table."""

from datetime import datetime, timedelta, timezone
import hashlib

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread

_PREFIX = "__lifecycle__"
_serializer = TypeSerializer()


def _recipient_hash(recipient: str) -> str:
    return hashlib.sha256(recipient.strip().lower().encode()).hexdigest()


def _ledger_key(recipient: str, message: str, window: str) -> str:
    digest = hashlib.sha256(f"{recipient.lower()}\0{message}\0{window}".encode()).hexdigest()
    return f"{_PREFIX}ledger#{digest}"


def _state_key(recipient: str) -> str:
    return f"{_PREFIX}recipient#{_recipient_hash(recipient)}"


def _av_map(value: dict) -> dict:
    return {key: _serializer.serialize(item) for key, item in value.items()}


async def get_recipient_state(recipient: str) -> dict | None:
    response = await run_in_thread(
        get_table("containers").get_item,
        Key={"owner_id": _state_key(recipient)},
        ConsistentRead=True,
    )
    return response.get("Item")


async def claim(
    *,
    recipient: str,
    message: str,
    eligibility_window: str,
    kind: str,
    reason: str,
    frequency_cap_hours: int | None,
    now: datetime,
) -> dict:
    """Atomically reserve one send and update its independent frequency bucket."""
    table = get_table("containers")
    ledger_key = _ledger_key(recipient, message, eligibility_window)
    state_key = _state_key(recipient)
    timestamp = now.astimezone(timezone.utc).isoformat()
    ledger = {
        "owner_id": ledger_key,
        "recipient_hash": _recipient_hash(recipient),
        "message": message,
        "eligibility_window": eligibility_window,
        "kind": kind,
        "reason": reason,
        "status": "claimed",
        "claimed_at": timestamp,
    }
    if kind == "lifecycle":
        cutoff = (now.astimezone(timezone.utc) - timedelta(hours=frequency_cap_hours or 72)).isoformat()
        update_expression = "SET last_lifecycle_sent_at = :now"
        condition = "attribute_not_exists(last_lifecycle_sent_at) OR last_lifecycle_sent_at <= :cutoff"
        values = _av_map({":now": timestamp, ":cutoff": cutoff})
    else:
        update_expression = "SET last_monthly_broadcast_at = :now"
        condition = None
        values = _av_map({":now": timestamp})

    update = {
        "TableName": table.table_name,
        "Key": _av_map({"owner_id": state_key}),
        "UpdateExpression": update_expression,
        "ExpressionAttributeValues": values,
    }
    if condition:
        update["ConditionExpression"] = condition
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {
                    "Put": {
                        "TableName": table.table_name,
                        "Item": _av_map(ledger),
                        "ConditionExpression": "attribute_not_exists(owner_id)",
                    }
                },
                {"Update": update},
            ],
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        existing = await run_in_thread(table.get_item, Key={"owner_id": ledger_key}, ConsistentRead=True)
        return {"claimed": False, "reason": "duplicate" if existing.get("Item") else "frequency_cap"}
    return {"claimed": True, "ledger_id": ledger_key.removeprefix(f"{_PREFIX}ledger#")}


async def record_result(
    *, ledger_id: str, status: str, provider_message_id: str | None, failure: str | None, sent_at: datetime | None
) -> dict:
    """Record provider outcome without changing the original claim identity."""
    key = f"{_PREFIX}ledger#{ledger_id}"
    values = {":status": status}
    updates = ["#status = :status"]
    names = {"#status": "status"}
    if provider_message_id is not None:
        values[":provider"] = provider_message_id
        updates.append("provider_message_id = :provider")
    if failure is not None:
        values[":failure"] = failure
        updates.append("failure = :failure")
    if sent_at is not None:
        values[":sent"] = sent_at.astimezone(timezone.utc).isoformat()
        updates.append("sent_at = :sent")
    await run_in_thread(
        get_table("containers").update_item,
        Key={"owner_id": key},
        UpdateExpression="SET " + ", ".join(updates),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(owner_id)",
    )
    return {"updated": True, "ledger_id": ledger_id}
