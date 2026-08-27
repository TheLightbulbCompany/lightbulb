"""WebSocket $connect handler — stores connection in DynamoDB, notifies backend."""
import os
import urllib.request
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])

_ws_internal_secret = ""


def _get_ws_internal_secret():
    """Fetch (and cache) the shared secret the backend /ws/* endpoints expect.

    Attached as the ``x-lightbulb-ws-internal`` header so the backend can tell this
    trusted Lambda apart from a forged caller arriving via the public HTTP API
    Gateway. Cached module-level on first success; on failure we return "" WITHOUT
    caching so a transient error doesn't permanently strip the header from a warm
    container. Returns "" when unconfigured (the backend then runs unenforced).
    The client is built lazily so module import never requires AWS region/creds.
    """
    global _ws_internal_secret
    if _ws_internal_secret:
        return _ws_internal_secret
    arn = os.environ.get("WS_INTERNAL_SECRET_SECRET_ARN", "")
    if not arn:
        return ""
    try:
        value = boto3.client("secretsmanager").get_secret_value(SecretId=arn).get("SecretString", "") or ""
    except Exception as e:
        logger.warning("Failed to fetch WS internal secret: %s", e)
        return ""
    _ws_internal_secret = value
    return value


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    authorizer = event["requestContext"].get("authorizer", {})
    user_id = authorizer.get("userId", "")
    org_id = authorizer.get("orgId", "")

    logger.info("WebSocket connect: connection_id=%s user_id=%s", connection_id, user_id)

    # Forward clientId from query for FastAPI ws_connect to recognize node-host
    # vs operator/control-ui connections. Only known values (node-host,
    # openclaw-macos) trigger the synthetic-challenge skip downstream; unknown
    # values are forwarded verbatim and ignored.
    qs = event.get("queryStringParameters") or {}
    client_id = qs.get("clientId")

    # Derive connectionType HERE (not only in the backend /ws/connect handler):
    # this Lambda writes the row BEFORE notifying the backend, and the owner-keyed
    # fan-out defaults a missing connectionType to "chat". Without this, a
    # node-host or paperclip-operator socket would look like a chat recipient
    # until the backend notification overwrites the row — and permanently if that
    # notify fails. Keep these client-id sets in sync with NODE_HOST_CLIENT_IDS /
    # PAPERCLIP_OPERATOR_CLIENT_ID in routers/websocket_chat.py.
    if client_id == "paperclip-operator":
        connection_type = "operator"
    elif client_id in ("node-host", "openclaw-macos"):
        connection_type = "node"
    else:
        connection_type = "chat"

    table.put_item(Item={
        "connectionId": connection_id,
        "userId": user_id,
        "orgId": org_id or "",
        "ownerId": org_id or user_id,  # owner = org for org members, else the user
        "connectionType": connection_type,
        "connectedAt": str(event["requestContext"].get("connectedAt", "")),
    })

    headers = {
        "x-connection-id": connection_id,
        "x-user-id": user_id,
        "x-org-id": org_id or "",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["x-client-id"] = client_id
    ws_secret = _get_ws_internal_secret()
    if ws_secret:
        headers["x-lightbulb-ws-internal"] = ws_secret

    try:
        req = urllib.request.Request(
            f"http://{os.environ['ALB_DNS_NAME']}/api/v1/ws/connect",
            method="POST",
            headers=headers,
            data=b"{}",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning("Failed to notify backend of connect: %s", e)

    return {"statusCode": 200}
