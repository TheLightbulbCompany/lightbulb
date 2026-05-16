"""WebSocket $connect handler — stores connection in DynamoDB, notifies backend."""
import os
import urllib.request
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONNECTIONS_TABLE"])


def handler(event, context):
    connection_id = event["requestContext"]["connectionId"]
    authorizer = event["requestContext"].get("authorizer", {})
    user_id = authorizer.get("userId", "")
    org_id = authorizer.get("orgId", "")

    logger.info("WebSocket connect: connection_id=%s user_id=%s", connection_id, user_id)

    table.put_item(Item={
        "connectionId": connection_id,
        "userId": user_id,
        "orgId": org_id or "",
        "connectedAt": str(event["requestContext"].get("connectedAt", "")),
    })

    # Forward clientId from query for FastAPI ws_connect to recognize node-host
    # vs operator/control-ui connections. Only known values (node-host,
    # openclaw-macos) trigger the synthetic-challenge skip downstream; unknown
    # values are forwarded verbatim and ignored.
    qs = event.get("queryStringParameters") or {}
    client_id = qs.get("clientId")

    headers = {
        "x-connection-id": connection_id,
        "x-user-id": user_id,
        "x-org-id": org_id or "",
        "Content-Type": "application/json",
    }
    if client_id:
        headers["x-client-id"] = client_id

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
