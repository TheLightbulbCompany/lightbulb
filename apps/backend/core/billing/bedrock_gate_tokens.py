"""Per-owner bedrock-gate bearer tokens — mint / rotate / deny.

The bedrock-gate Lambda authenticates every inbound Bedrock request by
SHA-256-hashing the caller's ``Authorization: Bearer <token>`` and GetItem-ing a
row in the per-env gate-tokens table keyed on that hash. This module is the
backend WRITER of those rows.

Hash-only storage: the plaintext token exists exactly ONCE — at mint time, when
it is returned to the caller for injection into the container's ECS
task-definition env (``AWS_BEARER_TOKEN_BEDROCK``). Nothing persists the
plaintext (mirrors ``api_key_repo`` / hook-token posture): a leaked table row
cannot be turned back into a usable token, and a compromised token is handled by
rotate (reprovision) or deny (kill switch), never by reading it back.

The token row's ``aip_tiers`` map is the INVERSE of the owner's
``bedrock_aip_arns`` map. The gateway authorizes + prices a request by looking
up the request's model-id — which is an AIP ARN — in ``aip_tiers`` to recover
its tier, so the map is keyed ``{aip_arn: tier}`` whereas the owner's containers
row stores ``{tier: aip_arn}``.

Table naming: the gate-tokens table is GATE-PRIVATE state, defined in the
bedrock-gate CDK stack (NOT database-stack), so — unlike ``bedrock-aip-owners``
/ ``metronome-fast-gate``, whose full names arrive as CDK-injected env vars —
the backend constructs it by convention from the env name, matching the gateway
Lambda's own ``TOKENS_TABLE`` and ``bedrock_aip._aip_name``'s ``isol8-{env}-``
scheme. An explicit ``BEDROCK_GATE_TOKENS_TABLE`` override wins (tests / manual
wiring).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time

import boto3

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import container_repo

logger = logging.getLogger(__name__)

# On rotation the PRIOR token row is not deleted outright — the currently-running
# ECS task still carries the old bearer token in its env and keeps serving during
# the reprovision's deploy window. Deleting immediately would 401 that live task
# until the replacement is up. Instead the old row gets a short TTL so it stays
# valid through the roll, then DynamoDB auto-reaps it. One hour comfortably
# outlives any task roll while bounding how long a superseded token lingers.
_ROTATION_GRACE_SECONDS = 3600


def _tokens_table_name() -> str:
    override = os.environ.get("BEDROCK_GATE_TOKENS_TABLE")
    if override:
        return override
    env = settings.ENVIRONMENT or "dev"
    return f"isol8-{env}-bedrock-gate-tokens"


def _tokens_table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_tokens_table_name())


def hash_token(token: str) -> str:
    """SHA-256 hex of the plaintext token — the gate-tokens table PK.

    MUST match the gateway Lambda's ``hashToken`` (``node:crypto`` SHA-256 hex of
    the raw bearer string) so a token minted here resolves there.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def mint_gate_token(owner_id: str, aip_arns: dict[str, str]) -> str:
    """Mint (rotating) a per-owner bedrock-gate bearer token; return the PLAINTEXT.

    - 32 random bytes, urlsafe-base64 (``secrets.token_urlsafe(32)``).
    - Writes a hash-keyed row ``{token_hash, owner_id, denied=False,
      aip_tiers={arn: tier}, created_at}``. ``aip_tiers`` is the INVERSE of the
      owner's ``{tier: arn}`` map — the gateway keys authorization + pricing on
      the AIP ARN.
    - Points the containers row's ``bedrock_gate_token_hash`` at the new hash.
      (No-ops on a not-yet-created row: the fresh-provision path folds the hash
      into ``create_user_service``'s row-creating upsert instead — see
      ``ecs_manager`` — mirroring how ``bedrock_role_arn`` / ``bedrock_aip_arns``
      are persisted.)
    - ROTATION: if the containers row already pointed at a prior hash, that old
      token row is given a short TTL (``_ROTATION_GRACE_SECONDS``) rather than
      deleted outright — the still-running ECS task keeps its old token valid
      through the reprovision's deploy window, then DynamoDB reaps the row.

    Only the hash is stored anywhere; the returned plaintext is the sole copy.
    """
    prior = await container_repo.get_by_owner_id(owner_id)
    prior_hash = prior.get("bedrock_gate_token_hash") if prior else None

    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    # Invert {tier: arn} -> {arn: tier}: the gateway looks a request's model-id
    # (an AIP ARN) up in this map to authorize + price it.
    aip_tiers = {arn: tier for tier, arn in (aip_arns or {}).items()}

    table = _tokens_table()
    await run_in_thread(
        table.put_item,
        Item={
            "token_hash": token_hash,
            "owner_id": owner_id,
            "denied": False,
            "aip_tiers": aip_tiers,
            "created_at": utc_now_iso(),
        },
    )
    await container_repo.update_fields(owner_id, {"bedrock_gate_token_hash": token_hash})

    if prior_hash and prior_hash != token_hash:
        # Grace-expire the superseded token instead of deleting it now, so the
        # still-running task it belongs to keeps working until the roll finishes.
        await run_in_thread(
            table.update_item,
            Key={"token_hash": prior_hash},
            UpdateExpression="SET #ttl = :exp",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":exp": int(time.time()) + _ROTATION_GRACE_SECONDS},
        )

    return token


async def deny_gate_token(owner_id: str, denied: bool) -> None:
    """Flip the ``denied`` flag on the owner's current gate-token row (kill switch).

    Looks up the containers row's ``bedrock_gate_token_hash`` and UpdateItems that
    token row's ``denied``. NO-OP when the owner has no token hash (nothing to
    deny) — callers (``revoke_owner_bedrock`` / ``restore_owner_bedrock`` and the
    meter Lambda kill path) treat this as a best-effort backstop alongside the
    IAM revoke, so the absence of a token must be silent, not an error.
    """
    row = await container_repo.get_by_owner_id(owner_id)
    token_hash = row.get("bedrock_gate_token_hash") if row else None
    if not token_hash:
        return
    table = _tokens_table()
    await run_in_thread(
        table.update_item,
        Key={"token_hash": token_hash},
        UpdateExpression="SET #d = :d",
        ExpressionAttributeNames={"#d": "denied"},
        ExpressionAttributeValues={":d": bool(denied)},
    )
