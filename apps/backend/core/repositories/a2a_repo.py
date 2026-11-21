"""The workspace's approval Rules document in the messaging table (``pk=A2A#{org}``, ``sk=RULES``)."""

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread
from core.repositories.messaging_repo import _table


async def get_rules(org: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"pk": f"A2A#{org}", "sk": "RULES"})
    item = resp.get("Item")
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "expiresAt")} if item else None


async def put_rules_if(org: str, rules: dict, read: dict | None) -> bool:
    """Store the document only if the stored one is still `read` (the document the caller read; None = none was
    stored), compared by its server revision. False when another write landed in between: the caller re-reads."""
    if read is None:
        cond: dict = {"ConditionExpression": "attribute_not_exists(sk)"}
    elif read.get("revision") is None:
        cond = {"ConditionExpression": "attribute_not_exists(#r)", "ExpressionAttributeNames": {"#r": "revision"}}
    else:
        cond = {
            "ConditionExpression": "#r = :r",
            "ExpressionAttributeNames": {"#r": "revision"},
            "ExpressionAttributeValues": {":r": read["revision"]},
        }
    try:
        await run_in_thread(_table().put_item, Item={**rules, "pk": f"A2A#{org}", "sk": "RULES"}, **cond)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True
