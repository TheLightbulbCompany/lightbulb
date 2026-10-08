"""AppSync Events Lambda authorizer for the `pub` namespace: a session link's live terminal.

The token is a lease `<publicId>.<key>` the backend minted for someone holding an "Anyone with
the link" link (session_links.viewing): 45 s, renewed every 15 s. While its row is unexpired it
opens connections, and ONE subscription, to `/pub/<publicId>` exactly (never a wildcard, never
another channel): the subscribe consumes it (`subscribedAt`, a conditional write), so a lease is
not a ticket for any number of sockets, and each further subscription needs a seat from the
backend's budget. Publishing is IAM only. Nothing is cached (ttlOverride 0) and nothing is logged:
a raised error is a refusal to AppSync.
"""

import hashlib
import os
import time

import boto3
from botocore.exceptions import ClientError

_table = boto3.resource("dynamodb").Table(os.environ["MESSAGING_TABLE"])


def _allowed(event: dict) -> bool:
    public, _, key = str(event.get("authorizationToken") or "").partition(".")
    context = event.get("requestContext") or {}
    operation = context.get("operation")
    if not public or not key or operation not in ("EVENT_CONNECT", "EVENT_SUBSCRIBE"):
        return False
    # EVENT_CONNECT carries no channel; a subscribe must name this lease's own.
    if operation == "EVENT_SUBSCRIBE" and context.get("channel") != f"/pub/{public}":
        return False
    seat = {"pk": f"PUBWATCH#{public}", "sk": f"VIEWER#{hashlib.sha256(key.encode()).hexdigest()}"}
    now = int(time.time())
    if operation == "EVENT_CONNECT":
        row = _table.get_item(Key=seat, ConsistentRead=True).get("Item")
        return bool(row) and int(row["expiresAt"]) > now
    try:
        _table.update_item(
            Key=seat,
            UpdateExpression="SET subscribedAt = :now",
            ConditionExpression="attribute_exists(pk) AND expiresAt > :now AND attribute_not_exists(subscribedAt)",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False  # unknown, lapsed, or already subscribed once
    return True


def handler(event, _context=None):
    return {"isAuthorized": _allowed(event), "ttlOverride": 0}
