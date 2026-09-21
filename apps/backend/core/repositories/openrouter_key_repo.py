"""Durable owner-key claims for the managed OpenRouter route."""

from botocore.exceptions import ClientError
import time

from core.dynamodb import get_table, run_in_thread


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


async def claim_new(owner_id: str, claim_id: str, *, now_sec: int) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").put_item,
            Item={"owner_id": owner_id, "claim_id": claim_id, "claimed_at": now_sec, "state": "pending"},
            ConditionExpression="attribute_not_exists(owner_id)",
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise


async def activate(owner_id: str, claim_id: str, key_hash: str, secret_name: str, workspace_id: str) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #state = :active, key_hash = :hash, secret_name = :secret, workspace_id = :workspace",
            ConditionExpression="#state = :pending AND claim_id = :claim AND attribute_not_exists(retire_requested_at)",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":active": "active",
                ":pending": "pending",
                ":claim": claim_id,
                ":hash": key_hash,
                ":secret": secret_name,
                ":workspace": workspace_id,
            },
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise


async def get(owner_id: str) -> dict | None:
    response = await run_in_thread(
        get_table("openrouter-keys").get_item,
        Key={"owner_id": owner_id},
        ConsistentRead=True,
    )
    return response.get("Item")


async def reclaim_stale(owner_id: str, claim_id: str, *, now_sec: int, ttl_sec: int) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET claim_id = :claim, claimed_at = :now",
            ConditionExpression=(
                "#state = :pending AND claimed_at <= :deadline AND attribute_not_exists(retire_requested_at)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":claim": claim_id,
                ":now": now_sec,
                ":pending": "pending",
                ":deadline": now_sec - ttl_sec,
            },
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise


async def drop_pending(owner_id: str, claim_id: str) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").delete_item,
            Key={"owner_id": owner_id},
            ConditionExpression="#state = :pending AND claim_id = :claim",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":pending": "pending", ":claim": claim_id},
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise


async def claim_disabled(owner_id: str, claim_id: str, *, now_sec: int) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET claim_id = :claim, claimed_at = :now, #state = :pending "
                "REMOVE key_hash, secret_name, retirement_requested_at, "
                "retirement_observed_at, retirement_processing_at, retire_requested_at"
            ),
            ConditionExpression="#state = :disabled",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":claim": claim_id,
                ":now": now_sec,
                ":pending": "pending",
                ":disabled": "disabled",
            },
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise


async def mark_retiring(owner_id: str) -> bool:
    for _ in range(4):
        row = await get(owner_id)  # strong read: choose the current state after a racing activation
        if not row or row.get("state") not in {"active", "pending"}:
            return False
        active = row["state"] == "active"
        try:
            await run_in_thread(
                get_table("openrouter-keys").update_item,
                Key={"owner_id": owner_id},
                UpdateExpression=(
                    "SET #state = :retiring, retirement_requested_at = :now"
                    if active
                    else "SET retire_requested_at = if_not_exists(retire_requested_at, :now)"
                ),
                ConditionExpression="#state = :expected",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":expected": row["state"],
                    ":now": int(time.time()),
                    **({":retiring": "retiring"} if active else {}),
                },
            )
            return True
        except ClientError as exc:
            if not _condition_failed(exc):
                raise
    raise RuntimeError("OpenRouter key retirement is contended; retry")


async def cancel_retirement(owner_id: str, key_hash: str) -> bool:
    try:
        await run_in_thread(
            get_table("openrouter-keys").update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET #state = :active REMOVE retirement_requested_at, retirement_observed_at, retirement_processing_at"
            ),
            ConditionExpression="#state = :retiring AND key_hash = :hash",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":active": "active",
                ":retiring": "retiring",
                ":hash": key_hash,
            },
        )
        return True
    except ClientError as exc:
        if _condition_failed(exc):
            return False
        raise
