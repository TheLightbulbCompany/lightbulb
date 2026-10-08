"""AppSync Events Lambda authorizer for the `pub` namespace: a session link's live terminal.

The token is a lease `<publicId>.<key>` the backend minted for someone holding an "Anyone with
the link" link (session_links.viewing): 45 s, renewed every 15 s. It opens a connection and one
subscription, to `/pub/<publicId>` exactly (never a wildcard, never another channel), while its
row is unexpired. Publishing is IAM only. Nothing is cached (ttlOverride 0) and nothing is logged:
a raised error is a refusal to AppSync.
"""

import hashlib
import os
import time

import boto3

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
    row = _table.get_item(
        Key={"pk": f"PUBWATCH#{public}", "sk": f"VIEWER#{hashlib.sha256(key.encode()).hexdigest()}"},
        ConsistentRead=True,
    ).get("Item")
    return bool(row) and int(row["expiresAt"]) > time.time()


def handler(event, _context=None):
    return {"isAuthorized": _allowed(event), "ttlOverride": 0}
