"""ChatGPT OAuth — device-code flow orchestration.

We use the public Codex CLI client_id `app_EMoamEEZ73f0CkXaXp7hrann`,
verified at github.com/openai/codex/blob/main/codex-rs/login/src/device_code_auth.rs.

OpenAI's actual `codex login --device-auth` flow:
  1. POST {base}/api/accounts/deviceauth/usercode {client_id} →
     returns {device_auth_id, user_code, interval}
  2. User opens {base}/codex/device in browser, signs in, enters user_code
  3. Backend polls POST {base}/api/accounts/deviceauth/token
     {device_auth_id, user_code} until it returns
     {authorization_code, code_challenge, code_verifier}
  4. Backend exchanges with POST {base}/oauth/token
     (grant_type=authorization_code, code, code_verifier,
      redirect_uri={base}/deviceauth/callback) → access + refresh tokens

base = https://auth.openai.com.

Spec §5.1 originally specified a generic OAuth 2.0 device-code flow at
/codex/device, which OpenAI deprecated for this client_id (~2026-04).
This module pins the actual endpoints the OpenAI CLI uses today.

auth.json shape on EFS is unchanged — only the orchestration changed.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Final

import boto3
import httpx
from botocore.exceptions import ClientError

from core.config import settings
from core.encryption import get_fernet


logger = logging.getLogger(__name__)


# Constants borrowed from OpenAI's Codex CLI (see module docstring).
CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL: Final = "https://auth.openai.com"
USER_CODE_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
TOKEN_POLL_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL: Final = f"{AUTH_BASE_URL}/oauth/token"
VERIFICATION_URL: Final = f"{AUTH_BASE_URL}/codex/device"
EXCHANGE_REDIRECT_URI: Final = f"{AUTH_BASE_URL}/deviceauth/callback"


class OAuthAlreadyActiveError(Exception):
    """Raised when start is called for a user who already has active
    tokens. Callers should reuse or revoke before starting a new flow."""


class OAuthExchangeFailedError(Exception):
    """Raised when OpenAI rejects the code exchange or device-code call."""


@dataclass(frozen=True)
class DeviceCodeResponse:
    """User-facing fields shown in our UI to drive completion.

    The backend-side device_auth_id is intentionally NOT exposed —
    it stays in DDB and is used by `poll_device_code` server-side."""

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePollResult:
    """Returned on successful poll. Tokens persisted internally."""

    account_id: str | None


# Sentinel returned while OpenAI says "still pending".
DevicePollPending: Final = object()


def _table():
    table_name = os.environ.get("OAUTH_TOKENS_TABLE") or settings.OAUTH_TOKENS_TABLE
    if not table_name:
        raise RuntimeError("OAUTH_TOKENS_TABLE is empty — backend is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(table_name)


async def request_device_code(*, user_id: str) -> DeviceCodeResponse:
    """Start a device-code session. Persists `device_auth_id` + `user_code`
    in DDB so the subsequent poll knows what to ask OpenAI about.

    Raises OAuthAlreadyActiveError if the user already has active tokens.
    """
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.post(
            USER_CODE_URL,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"client_id": CLIENT_ID}),
        )
    if resp.status_code >= 400:
        body_preview = resp.text[:300] if resp.text else ""
        logger.warning(
            "Device-code usercode request returned %d for user %s: %s",
            resp.status_code,
            user_id,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI device-code usercode request failed: {resp.status_code}")

    body = resp.json()
    device_auth_id = body.get("device_auth_id")
    user_code = body.get("user_code") or body.get("usercode")
    interval = int(body.get("interval", 5))
    if not device_auth_id or not user_code:
        logger.warning("Usercode response missing fields for user %s: %s", user_id, body.keys())
        raise OAuthExchangeFailedError("OpenAI usercode response missing required fields")

    try:
        _table().put_item(
            Item={
                "user_id": user_id,
                "state": "pending",
                "device_auth_id": device_auth_id,
                "user_code": user_code,
                "interval": interval,
            },
            ConditionExpression="attribute_not_exists(user_id) OR #s <> :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":active": "active"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise OAuthAlreadyActiveError(
                f"User {user_id} already has active OAuth tokens — revoke before starting again"
            ) from None
        raise

    return DeviceCodeResponse(
        user_code=user_code,
        verification_uri=VERIFICATION_URL,
        # OpenAI's device-code spec gives a 15-minute window per the CLI
        # source. Body doesn't echo this back so we hardcode the same
        # value the CLI prints to the user.
        expires_in=15 * 60,
        interval=interval,
    )


async def poll_device_code(*, user_id: str) -> DevicePollResult | object:
    """Poll OpenAI's deviceauth/token endpoint for this user's session.

    Returns DevicePollPending while the user hasn't completed sign-in
    (OpenAI returns 403/404 in that case). Returns DevicePollResult on
    success after Fernet-encrypting access+refresh tokens into DDB.
    Raises OAuthExchangeFailedError on terminal errors.
    """
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "pending":
        raise OAuthExchangeFailedError(f"No pending device-code session for user {user_id}")
    # Legacy pending rows (from the pre-Codex-CLI flow) store `device_code`
    # instead of `device_auth_id`. Same for any partially-written row that
    # doesn't have user_code. Treat both as stale — the new flow can't
    # resume them. Caller surfaces this as a clean error; user clicks
    # "Try again" which calls /start, which overwrites the pending row
    # with a fresh device_auth_id (the put_item ConditionExpression only
    # blocks state=active, not state=pending).
    device_auth_id = row.get("device_auth_id")
    user_code = row.get("user_code")
    if not device_auth_id or not user_code:
        raise OAuthExchangeFailedError(
            f"Stale OAuth session for user {user_id} (legacy row format) — click Connect ChatGPT again to restart"
        )

    # Step 1: ask OpenAI's deviceauth/token whether the user has
    # completed sign-in. While pending, OpenAI returns 403 or 404 per
    # the CLI source.
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.post(
            TOKEN_POLL_URL,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                }
            ),
        )

    if resp.status_code in (403, 404):
        return DevicePollPending
    if resp.status_code >= 400:
        body_preview = resp.text[:300] if resp.text else ""
        logger.warning(
            "Device-code token poll for user %s returned %d: %s",
            user_id,
            resp.status_code,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI device-code token poll failed: {resp.status_code}")

    code_resp = resp.json()
    auth_code = code_resp.get("authorization_code")
    verifier = code_resp.get("code_verifier")
    if not auth_code or not verifier:
        logger.warning("Token poll response missing fields for user %s: %s", user_id, code_resp.keys())
        raise OAuthExchangeFailedError("OpenAI token poll response missing required fields")

    # Step 2: exchange the authorization_code for access+refresh tokens
    # against the standard OAuth /oauth/token endpoint, using the
    # PKCE verifier OpenAI just gave us.
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        token_resp = await client.post(
            OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": EXCHANGE_REDIRECT_URI,
            },
        )

    if token_resp.status_code >= 400:
        body_preview = token_resp.text[:300] if token_resp.text else ""
        logger.warning(
            "OAuth token exchange for user %s returned %d: %s",
            user_id,
            token_resp.status_code,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI token exchange failed: {token_resp.status_code}")

    body = token_resp.json()
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not access_token or not refresh_token:
        logger.warning("Token response missing fields for user %s: %s", user_id, body.keys())
        raise OAuthExchangeFailedError("OpenAI token response missing required fields")

    # account_id can be in the body OR encoded in the JWT id_token. Try
    # both — pi-mono and the CLI both decode the JWT for the
    # chatgpt_account_id claim under https://api.openai.com/auth.
    account_id = body.get("account_id") or _extract_account_id(access_token)

    tokens_plain = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": body.get("id_token"),
            "account_id": account_id,
        }
    ).encode()
    encrypted = get_fernet().encrypt(tokens_plain)

    _table().update_item(
        Key={"user_id": user_id},
        UpdateExpression=(
            "SET #s = :ok, encrypted_tokens = :tok, account_id = :acc REMOVE device_auth_id, user_code, #i"
        ),
        ExpressionAttributeNames={"#s": "state", "#i": "interval"},
        ExpressionAttributeValues={
            ":ok": "active",
            ":tok": encrypted,
            ":acc": account_id or "",
        },
    )
    return DevicePollResult(account_id=account_id)


def _extract_account_id(access_token: str) -> str | None:
    """Decode the JWT and pull chatgpt_account_id from the auth claim.

    Mirrors the CLI's logic. JWT validation isn't done here — OpenAI
    just issued this token to us via TLS, so we trust it for the
    purpose of pulling out an opaque identifier.
    """
    import base64

    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        # Pad payload to a multiple of 4 for base64 decode
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        claims = json.loads(decoded)
        auth = claims.get("https://api.openai.com/auth") or {}
        account_id = auth.get("chatgpt_account_id")
        return str(account_id) if account_id else None
    except Exception:
        logger.debug("Failed to extract account_id from JWT", exc_info=True)
        return None


async def get_decrypted_tokens(*, user_id: str) -> dict | None:
    """Decrypt and return the user's stored OAuth tokens. None if no row."""
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "active":
        return None
    plain = get_fernet().decrypt(bytes(row["encrypted_tokens"]))
    return json.loads(plain.decode())


async def get_decrypted_tokens_consistent(*, user_id: str) -> dict | None:
    """Strongly-consistent variant of ``get_decrypted_tokens``.

    Used by ``GET /oauth/chatgpt/status`` so the wizard reads its own
    write from ``/poll`` without an eventual-consistency stutter. Other
    callers (gate evaluator, provisioning) keep using the eventually-
    consistent helper to avoid taxing every read path with strong reads.
    """
    row = _table().get_item(Key={"user_id": user_id}, ConsistentRead=True).get("Item")
    if not row or row.get("state") != "active":
        return None
    plain = get_fernet().decrypt(bytes(row["encrypted_tokens"]))
    return json.loads(plain.decode())


async def revoke_user_oauth(*, user_id: str) -> None:
    """Delete the user's OAuth row. Caller is responsible for also
    deleting any pre-staged auth file on EFS (see workspace.py)."""
    _table().delete_item(Key={"user_id": user_id})
