"""WebSocket $disconnect handler — removes connection from DynamoDB, notifies backend."""
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
    Gateway. Cached on first success; on failure returns "" WITHOUT caching so a
    transient error doesn't permanently strip the header from a warm container.
    Returns "" when unconfigured (the backend then runs unenforced). The client
    is built lazily so module import never requires AWS region/creds.
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

    logger.info("WebSocket disconnect: connection_id=%s", connection_id)

    try:
        table.delete_item(Key={"connectionId": connection_id})
    except Exception as e:
        logger.warning("Failed to remove connection from DynamoDB: %s", e)

    headers = {
        "x-connection-id": connection_id,
        "Content-Type": "application/json",
    }
    ws_secret = _get_ws_internal_secret()
    if ws_secret:
        headers["x-lightbulb-ws-internal"] = ws_secret

    try:
        req = urllib.request.Request(
            f"http://{os.environ['ALB_DNS_NAME']}/api/v1/ws/disconnect",
            method="POST",
            headers=headers,
            data=b"{}",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning("Failed to notify backend of disconnect: %s", e)

    return {"statusCode": 200}
