"""A2A state in the messaging table (``pk=A2A#{org}``)."""

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
