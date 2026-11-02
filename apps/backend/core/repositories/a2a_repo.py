"""A2A state in the messaging table (``pk=A2A#{org}``)."""

import uuid

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread
from core.repositories.messaging_repo import _table

AUTHORITY_ROW_TTL_S = 86_400


async def claim_authority_id(org: str, authority_id: str, now: int) -> bool:
    """Record an authority object's id once per org; False when it was already claimed (a replay)."""
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": f"A2A#{org}", "sk": f"AUTH#{authority_id}", "expiresAt": now + AUTHORITY_ROW_TTL_S},
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


async def get_rules(org: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": "RULES"})
    item = resp.get("Item")
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "expiresAt")} if item else None


async def put_rules(org: str, rules: dict) -> None:
    await run_in_thread(_table().put_item, Item={**rules, "pk": f"A2A#{org}", "sk": "RULES"})


async def put_exception(org: str, resource: str, operands_hash: str, *, holder: str, task_id: str, now: int) -> str:
    """Record a holder's one-day approval for this resource and operands; returns the exception id."""
    exc_id = str(uuid.uuid4())
    await run_in_thread(
        _table().put_item,
        Item={
            "pk": f"A2A#{org}",
            "sk": f"EXC#{resource}#{operands_hash}",
            "id": exc_id,
            "holder": holder,
            "taskId": task_id,
            "expiresAt": now + AUTHORITY_ROW_TTL_S,
        },
    )
    return exc_id


async def get_exception(org: str, resource: str, operands_hash: str, now: int) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": f"EXC#{resource}#{operands_hash}"})
    item = resp.get("Item")
    return item if item and int(item["expiresAt"]) > now else None
