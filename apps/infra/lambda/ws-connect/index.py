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
    qs = event.get("queryStringParameters") or {}
    client_id = qs.get("clientId")
    auth_kind = authorizer.get("authKind", "")
    device_id = authorizer.get("deviceId", "")
    if not user_id:
        return {"statusCode": 403}
    if auth_kind == "device":
        if client_id not in ("node-host", "openclaw-macos", "lightbulb-local-sessions") or not device_id:
            return {"statusCode": 403}
        connection_type = "local-session" if client_id == "lightbulb-local-sessions" else "node"
    elif auth_kind == "paperclip_service":
        if client_id != "paperclip-operator" or device_id:
            return {"statusCode": 403}
        connection_type = "operator"
    else:
        # Clerk browser sessions, missing provenance, unknown client IDs, and
        # cross-kind combinations all fail before a connection row is written.
        return {"statusCode": 403}

    table.put_item(
        Item={
            "connectionId": connection_id,
            "userId": user_id,
            "orgId": org_id or "",
            "ownerId": org_id or user_id,
            "connectionType": connection_type,
            "authKind": auth_kind,
            "clientId": client_id,
            **({"deviceId": device_id} if auth_kind == "device" else {}),
            "connectedAt": str(event["requestContext"].get("connectedAt", "")),
        }
    )

    headers = {
        "x-connection-id": connection_id,
        "x-user-id": user_id,
        "x-org-id": org_id or "",
        "x-auth-kind": auth_kind,
        "x-client-id": client_id,
        "Content-Type": "application/json",
    }
    if auth_kind == "device":
        headers["x-verified-device-id"] = device_id
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
        if connection_type == "local-session":
            # Unlike node/operator startup, canonical mac enrollment is admission.
            # A failed handshake must not leave a usable local-session connection.
            table.delete_item(Key={"connectionId": connection_id})
            return {"statusCode": 403 if getattr(e, "code", None) == 403 else 503}
        logger.warning("Failed to notify backend of connect: %s", e)

    return {"statusCode": 200}
