"""WebSocket $default handler — forwards message body to backend via ALB."""
import os
import urllib.request
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
    body = event.get("body", "") or ""

    headers = {
        "x-connection-id": connection_id,
        "Content-Type": "application/json",
    }
    ws_secret = _get_ws_internal_secret()
    if ws_secret:
        headers["x-lightbulb-ws-internal"] = ws_secret

    try:
        req = urllib.request.Request(
            f"http://{os.environ['ALB_DNS_NAME']}/api/v1/ws/message",
            method="POST",
            headers=headers,
            data=body.encode("utf-8") if body else b"{}",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return {"statusCode": resp.status}
    except Exception as e:
        logger.error("Failed to forward message: %s", e)
        return {"statusCode": 500}
