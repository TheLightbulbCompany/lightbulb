"""
Machine-only Lambda authorizer for WebSocket API Gateway.

Accepts one credential kind: paired desktop device JWTs (EdDSA, kind=device),
verified against the signature-bound public key in the owner's container row.
This door now admits only the desktop local-session relay (clientId is
checked downstream by ws-connect); Paperclip operators and Tauri node hosts
authenticate over the raw WS relay instead (routers/gateway_relay.py) and
verify their own (still HS256) service tokens there via core/services/
machine_auth.py — not through this Lambda.

Clerk browser sessions are intentionally not accepted. Human commands use HTTP
and live browser delivery uses AppSync Events.
"""

import base64
import logging
import os
from typing import Any, Optional

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Desktop device-token configuration
DEVICE_TOKEN_KIND = "device"
# Authorizer-enforced ceiling on a device token's self-declared lifetime
# (exp - iat). The node mints ~90s tokens; 300s leaves headroom while bounding
# the replay window regardless of what a compromised/buggy client mints.
DEVICE_TOKEN_MAX_LIFETIME_S = 300
CONTAINERS_TABLE = os.environ.get("CONTAINERS_TABLE", "")

# Boto DynamoDB Table for the containers row (device pubkey registry), lazy
_containers_table = None


def _get_containers_table():
    global _containers_table
    if _containers_table is None and CONTAINERS_TABLE:
        _containers_table = boto3.resource("dynamodb").Table(CONTAINERS_TABLE)
    return _containers_table


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


def _try_device_token(token: str) -> Optional[dict]:
    """Validate a desktop device-key WS-upgrade token (EdDSA, kind=device).

    The desktop node-host self-mints a short-lived JWT signed by its Ed25519
    Keychain key on every (re)connect. We verify PROOF-OF-POSSESSION against the
    device's registered public key, projected onto the owner's ``containers`` row
    at ``/desktop/pair`` as ``paired_devices[deviceId]`` — no long-lived bearer
    secret at rest, and revocation is un-pairing (the row entry disappears → Deny
    on the next connect). Returns verified user/org/auth/device provenance on
    success, else None. Fails CLOSED.

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
        return None  # not a device token
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

    Validates a paired-device credential. Every other token, including a
    valid Clerk browser session or a Paperclip service token (which now
    authenticates over the raw WS relay instead — see module docstring), is
    denied. clientId narrowing (this door admits only the desktop
    local-session relay) happens downstream in the ws-connect $connect
    handler, which sees the same verified context this policy carries.

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
    device_claims = _try_device_token(token)
    if device_claims:
        logger.info(f"Authorized via device token user_id={device_claims['userId']}")
        return generate_policy(
            principal_id=device_claims["userId"],
            effect="Allow",
            resource=method_arn,
            context=device_claims,
        )

    logger.warning("Token is not an admitted machine credential")
    return generate_policy("unauthorized", "Deny", method_arn)
