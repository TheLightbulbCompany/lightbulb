"""
Machine-only Lambda authorizer for WebSocket API Gateway.

Accepts two existing credential kinds:

1. Paired desktop device JWTs (EdDSA, kind=device), verified against the
   signature-bound public key in the owner's container row.
2. Paperclip service-token JWTs (HS256, kind=paperclip_service), verified with
   the environment signing key from Secrets Manager.

Clerk browser sessions are intentionally not accepted. Human commands use HTTP
and live browser delivery uses AppSync Events.
"""

import base64
import json
import logging
import os
import time
from typing import Any, Optional

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Paperclip service-token configuration
SERVICE_TOKEN_KIND = "paperclip_service"
PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN = os.environ.get("PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN", "")

# Desktop device-token configuration
DEVICE_TOKEN_KIND = "device"
# Authorizer-enforced ceiling on a device token's self-declared lifetime
# (exp - iat). The node mints ~90s tokens; 300s leaves headroom while bounding
# the replay window regardless of what a compromised/buggy client mints.
DEVICE_TOKEN_MAX_LIFETIME_S = 300
CONTAINERS_TABLE = os.environ.get("CONTAINERS_TABLE", "")

# Boto client for Secrets Manager (initialized lazily)
_secrets_client = None

# Boto DynamoDB Table for the containers row (device pubkey registry), lazy
_containers_table = None


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _get_containers_table():
    global _containers_table
    if _containers_table is None and CONTAINERS_TABLE:
        _containers_table = boto3.resource("dynamodb").Table(CONTAINERS_TABLE)
    return _containers_table


def _emit_metric(metric_name: str, value: int) -> None:
    """Emit a CloudWatch EMF metric to stdout.

    Lambda's log driver picks up Embedded Metric Format JSON automatically
    and publishes the metric without an explicit PutMetricData call. We use
    this so an alarm can fire on `ServiceTokenKeyLoadFailure > 0` without
    needing to wire CloudWatch SDK creds into the Lambda role.
    """
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "LightbulbAI/Authorizer",
                            "Dimensions": [["FunctionName"]],
                            "Metrics": [{"Name": metric_name, "Unit": "Count"}],
                        }
                    ],
                },
                "FunctionName": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown"),
                metric_name: value,
            }
        )
    )


def _load_service_token_key() -> str:
    """Fetch the Paperclip service-token signing key from Secrets Manager.

    Called once at cold-start. Returns "" on missing config or fetch failure;
    service-token attempts then fail closed.
    """
    arn = PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN
    if not arn:
        return ""
    try:
        resp = _get_secrets_client().get_secret_value(SecretId=arn)
        return resp.get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001 — best-effort cold-start fetch
        # Use error (not warning) so log-metric-filters and alarms can pick
        # up the signal — a silent fall-through here disables service tokens
        # platform-wide with no other observable symptom.
        logger.error(f"Failed to load service-token signing key: {e}")
        return ""


# Cold-start: load the symmetric key once per Lambda container
PAPERCLIP_SERVICE_TOKEN_KEY = _load_service_token_key()
_emit_metric(
    "ServiceTokenKeyLoadSuccess" if PAPERCLIP_SERVICE_TOKEN_KEY else "ServiceTokenKeyLoadFailure",
    1,
)


def generate_policy(principal_id: str, effect: str, resource: str, context: dict = None) -> dict:
    """
    Generate IAM policy document for WebSocket API authorizer.

    WebSocket APIs require IAM policy format (unlike HTTP APIs which use isAuthorized).
    """
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource,
                }
            ],
        },
    }
    if context:
        policy["context"] = context
    return policy


def _try_service_token(token: str) -> Optional[dict]:
    """Validate a Paperclip service-token JWT.

    Returns the claims dict if the token is valid; otherwise returns None.
    """
    if not PAPERCLIP_SERVICE_TOKEN_KEY:
        return None
    try:
        claims = jwt.decode(
            token,
            PAPERCLIP_SERVICE_TOKEN_KEY,
            algorithms=["HS256"],
        )
    except jwt.PyJWTError:
        return None
    if claims.get("kind") != SERVICE_TOKEN_KIND:
        return None
    if not claims.get("sub"):
        return None
    return claims


def _try_device_token(token: str) -> Optional[dict]:
    """Validate a desktop device-key WS-upgrade token (EdDSA, kind=device).

    The desktop node-host self-mints a short-lived JWT signed by its Ed25519
    Keychain key on every (re)connect. We verify PROOF-OF-POSSESSION against the
    device's registered public key, projected onto the owner's ``containers`` row
    at ``/desktop/pair`` as ``paired_devices[deviceId]`` — no long-lived bearer
    secret at rest, and revocation is un-pairing (the row entry disappears → Deny
    on the next connect). Returns verified user/org/auth/device provenance on success,
    else None so the caller can try the service-token branch. Fails CLOSED.

    Security note: the deviceId/owner claims are read UNVERIFIED only to locate
    the registered pubkey; the signature is then verified against THAT pubkey, so
    a forged deviceId/owner points at a key the attacker cannot sign for and the
    verification fails. Returns verified machine provenance on success, else None.
    The member identity is taken from the server-authored row, never self-asserted.
    Fails CLOSED.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") != "EdDSA":
        return None  # not a device token — let the other branches try
    if not CONTAINERS_TABLE:
        logger.warning("Device (EdDSA) token seen but CONTAINERS_TABLE not set")
        return None
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        if unverified.get("kind") != DEVICE_TOKEN_KIND:
            return None
        device_id = unverified.get("deviceId")
        owner_id = unverified.get("owner")
        if not device_id or not owner_id:
            return None

        resp = _get_containers_table().get_item(
            Key={"owner_id": owner_id}, ConsistentRead=True
        )
        item = resp.get("Item")
        if not item:
            return None
        entry = (item.get("paired_devices") or {}).get(device_id)
        if not entry or not entry.get("publicKey"):
            return None  # not paired / revoked

        pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(entry["publicKey"] + "=="))
        # Verify the EdDSA signature over the exact claims + enforce exp.
        claims = jwt.decode(
            token,
            key=pub,
            algorithms=["EdDSA"],
            options={
                "require": ["exp", "iat", "deviceId", "owner"],
                "verify_exp": True,
            },
            leeway=30,
        )
        # Bound the replay window at the trust boundary: refuse a token that
        # declares a lifetime beyond the ceiling, whatever the client minted.
        if claims["exp"] - claims["iat"] > DEVICE_TOKEN_MAX_LIFETIME_S:
            logger.warning("Device token lifetime exceeds cap; refusing")
            return None

        member_user_id = entry.get("userId")
        if not member_user_id:
            # Fail CLOSED. An entry with no attributed member must never be
            # allowed as the owner — that would masquerade an org id as a
            # personal user identity. The sole writer always sets userId.
            logger.warning("Device entry missing userId; refusing")
            return None
        # Personal owner: owner_id == member → orgId "" (mirrors Clerk personal).
        org_id = "" if owner_id == member_user_id else owner_id
        return {
            "userId": member_user_id,
            "orgId": org_id,
            "authKind": DEVICE_TOKEN_KIND,
            "deviceId": claims["deviceId"],
        }
    except jwt.PyJWTError as e:
        logger.warning(f"Device token rejected: {e}")
        return None
    except Exception as e:
        logger.exception(f"Device token validation error: {e}")
        return None


def handler(event: dict, context: Any) -> dict:
    """
    Lambda authorizer handler for WebSocket API.

    Validates paired device and Paperclip service credentials. Every other token,
    including a valid Clerk browser session, is denied.

    Args:
        event: API Gateway authorizer event containing:
            - queryStringParameters: {token: "..."}
            - methodArn: Resource ARN for policy
        context: Lambda context (unused)

    Returns:
        IAM policy document (WebSocket APIs require this format, not isAuthorized).
    """
    logger.info("Authorizer invoked")

    # methodArn is used as the resource in the policy
    method_arn = event.get("methodArn", "*")

    # Extract token from query parameters
    query_params = event.get("queryStringParameters") or {}
    token = query_params.get("token")

    if not token:
        logger.warning("No token provided in query parameters")
        return generate_policy("unauthorized", "Deny", method_arn)

    # --- Try desktop device token (EdDSA, proof-of-possession) ---
    # Routed by alg=EdDSA; a no-op (returns None) for HS256/RS256 tokens.
    device_claims = _try_device_token(token)
    if device_claims:
        logger.info(f"Authorized via device token user_id={device_claims['userId']}")
        return generate_policy(
            principal_id=device_claims["userId"],
            effect="Allow",
            resource=method_arn,
            context=device_claims,
        )

    # --- Try Paperclip service token (cheap symmetric verify) ---
    service_claims = _try_service_token(token)
    if service_claims:
        user_id = service_claims["sub"]
        logger.info(f"Authorized via service token user_id={user_id}")
        return generate_policy(
            principal_id=user_id,
            effect="Allow",
            resource=method_arn,
            context={
                "userId": user_id,
                "orgId": "",
                "authKind": SERVICE_TOKEN_KIND,
            },
        )

    logger.warning("Token is not an admitted machine credential")
    return generate_policy("unauthorized", "Deny", method_arn)
