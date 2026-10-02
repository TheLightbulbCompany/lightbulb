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

    # clientId is untrusted query input. It is admitted only when it matches the
    # already-verified credential kind from the Lambda authorizer context.
    #
    # This door now admits ONLY the desktop local-session relay. Tauri node
    # hosts and Paperclip operators dial the raw WS relay instead
    # (routers/gateway_relay.py, wss://bv[-env].thelightbulb.company/api/v1/gateway/ws)
    # and never reach $connect any more.
    qs = event.get("queryStringParameters") or {}
    client_id = qs.get("clientId")
    auth_kind = authorizer.get("authKind", "")
    device_id = authorizer.get("deviceId", "")
    if not user_id:
        return {"statusCode": 403}
    if auth_kind != "device" or client_id != "lightbulb-local-sessions" or not device_id:
        # Clerk browser sessions, missing provenance, unknown client IDs, and
        # any other client id all fail before a connection row is written.
        return {"statusCode": 403}
    connection_type = "local-session"

    table.put_item(
        Item={
            "connectionId": connection_id,
            "userId": user_id,
            "orgId": org_id or "",
            "ownerId": org_id or user_id,
            "connectionType": connection_type,
            "authKind": auth_kind,
            "clientId": client_id,
            "deviceId": device_id,
            "connectedAt": str(event["requestContext"].get("connectedAt", "")),
        }
    )

    headers = {
        "x-connection-id": connection_id,
        "x-user-id": user_id,
        "x-org-id": org_id or "",
        "x-auth-kind": auth_kind,
        "x-client-id": client_id,
        "x-verified-device-id": device_id,
        "Content-Type": "application/json",
    }
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
        # Canonical mac enrollment is admission — a failed handshake must not
        # leave a usable local-session connection.
        table.delete_item(Key={"connectionId": connection_id})
        return {"statusCode": 403 if getattr(e, "code", None) == 403 else 503}

    return {"statusCode": 200}
