"""
Lambda authorizer for WebSocket API Gateway.

Validates two kinds of bearer tokens for WebSocket $connect:

1. Paperclip service-token JWTs (HS256, kind=paperclip_service)
   — Long-lived JWTs minted by the FastAPI backend (core/services/service_token.py),
     signed with a symmetric secret stored in Secrets Manager
     (isol8/{env}/paperclip_service_token_key). Used by Paperclip agents to
     reach a specific user's OpenClaw container via the existing WebSocket
     gateway. Checked FIRST because symmetric verify is far cheaper than
     a JWKS round-trip.

2. Clerk JWTs (RS256, browser sessions)
   — Standard end-user browser auth. Verified against Clerk's JWKS.

Returns IAM policy (WebSocket APIs require this format, not isAuthorized) plus
authorization context (userId, orgId, authKind) for the backend.
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
from jwt import PyJWKClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Clerk configuration from environment
CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL", "")
CLERK_ISSUER = os.environ.get("CLERK_ISSUER", "")
# SEC-8 token-origin fence. Clerk session tokens carry NO `aud` claim; the
# origin check is `azp` (authorized party — the frontend origin that minted
# the token). Comma-separated allowlist; empty disables the check. Native-app
# tokens have no `azp` and always pass (Clerk SDK semantics).
CLERK_AUTHORIZED_PARTIES = frozenset(
    p.strip() for p in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",") if p.strip()
)
# "audit" → log + EMF metric on mismatch, never deny; "enforce" → deny.
CLERK_AZP_MODE = os.environ.get("CLERK_AZP_MODE", "audit")

# Paperclip service-token configuration
SERVICE_TOKEN_KIND = "paperclip_service"
PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN = os.environ.get(
    "PAPERCLIP_SERVICE_TOKEN_KEY_SECRET_ARN", ""
)

# Desktop device-token configuration
DEVICE_TOKEN_KIND = "device"
# Authorizer-enforced ceiling on a device token's self-declared lifetime
# (exp - iat). The node mints ~90s tokens; 300s leaves headroom while bounding
# the replay window regardless of what a compromised/buggy client mints.
DEVICE_TOKEN_MAX_LIFETIME_S = 300
CONTAINERS_TABLE = os.environ.get("CONTAINERS_TABLE", "")

# Cache JWKS client (reused across invocations)
_jwks_client = None

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
                            "Namespace": "Isol8/Authorizer",
                            "Dimensions": [["FunctionName"]],
                            "Metrics": [{"Name": metric_name, "Unit": "Count"}],
                        }
                    ],
                },
                "FunctionName": os.environ.get(
                    "AWS_LAMBDA_FUNCTION_NAME", "unknown"
                ),
                metric_name: value,
            }
        )
    )


def _load_service_token_key() -> str:
    """Fetch the Paperclip service-token signing key from Secrets Manager.

    Called once at cold-start. Returns "" on missing config or fetch failure
    (the service-token branch will then short-circuit to None and the
    Clerk path runs unchanged).
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
    "ServiceTokenKeyLoadSuccess"
    if PAPERCLIP_SERVICE_TOKEN_KEY
    else "ServiceTokenKeyLoadFailure",
    1,
)


def get_jwks_client() -> PyJWKClient:
    """Get or create cached JWKS client."""
    global _jwks_client
    if _jwks_client is None:
        if not CLERK_JWKS_URL:
            raise ValueError("CLERK_JWKS_URL environment variable not set")
        _jwks_client = PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
    return _jwks_client


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

    Returns the claims dict if the token is a valid service token; returns
    None if the token is not a service token (so the caller can fall through
    to the Clerk JWT path) or if signing-key config is missing.
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
    on the next connect). Returns {userId, orgId, authKind} on success, else None
    so the caller falls through to the service-token / Clerk paths. Fails CLOSED.

    Security note: the deviceId/owner claims are read UNVERIFIED only to locate
    the registered pubkey; the signature is then verified against THAT pubkey, so
    a forged deviceId/owner points at a key the attacker cannot sign for and the
    verification fails. The member identity is taken from the server-authored row
    (paired_devices[deviceId].userId), never self-asserted by the device.
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

        resp = _get_containers_table().get_item(Key={"owner_id": owner_id})
        item = resp.get("Item")
        if not item:
            return None
        entry = (item.get("paired_devices") or {}).get(device_id)
        if not entry or not entry.get("publicKey"):
            return None  # not paired / revoked

        pub = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(entry["publicKey"] + "==")
        )
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
        }
    except jwt.PyJWTError as e:
        logger.warning(f"Device token rejected: {e}")
        return None
    except Exception as e:
        logger.exception(f"Device token validation error: {e}")
        return None


def _azp_allowed(payload: dict) -> bool:
    """SEC-8: check the Clerk token's `azp` claim against known origins.

    Returns False only on a mismatch while CLERK_AZP_MODE=enforce. A missing
    `azp` (native apps) or an empty allowlist passes; an audit-mode mismatch
    logs + emits the AzpMismatch EMF metric but still passes.
    """
    azp = payload.get("azp")
    if not azp or not CLERK_AUTHORIZED_PARTIES or azp in CLERK_AUTHORIZED_PARTIES:
        return True
    logger.warning(
        f"JWT azp {azp!r} not in CLERK_AUTHORIZED_PARTIES (mode={CLERK_AZP_MODE})"
    )
    _emit_metric("AzpMismatch", 1)
    return CLERK_AZP_MODE != "enforce"


def handler(event: dict, context: Any) -> dict:
    """
    Lambda authorizer handler for WebSocket API.

    Validates Paperclip service tokens (cheap symmetric verify, checked first)
    and Clerk JWTs (RS256 via JWKS).

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

    # --- Try Paperclip service token first (cheap symmetric verify) ---
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

    # --- Validate Clerk JWT ---
    try:
        # Get signing key from JWKS
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        # Decode and validate JWT
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            options={
                "verify_exp": True,
                "verify_iss": True,
                # Clerk session tokens carry no `aud`; the origin check is the
                # `azp` claim, validated below (SEC-8).
                "verify_aud": False,
            }
        )

        if not _azp_allowed(payload):
            return generate_policy("unauthorized", "Deny", method_arn)

        # Extract user and org info
        user_id = payload.get("sub")
        # Clerk v2 JWTs nest org claims under "o" key: {"id", "rol", "slg", "per"}
        org_claims = payload.get("o", {})
        org_id = org_claims.get("id") if isinstance(org_claims, dict) else None

        if not user_id:
            logger.warning("Token missing 'sub' claim")
            return generate_policy("unauthorized", "Deny", method_arn)

        logger.info(f"Authorized user_id={user_id}, org_id={org_id or 'personal'}")

        # Return IAM policy with Allow effect and user context
        return generate_policy(
            principal_id=user_id,
            effect="Allow",
            resource=method_arn,
            context={
                "userId": user_id,
                "orgId": org_id or "",
                "authKind": "clerk_user",
            }
        )

    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return generate_policy("unauthorized", "Deny", method_arn)
    except jwt.InvalidIssuerError:
        logger.warning("Invalid token issuer")
        return generate_policy("unauthorized", "Deny", method_arn)
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return generate_policy("unauthorized", "Deny", method_arn)
    except Exception as e:
        logger.exception(f"Unexpected error validating token: {e}")
        return generate_policy("unauthorized", "Deny", method_arn)
