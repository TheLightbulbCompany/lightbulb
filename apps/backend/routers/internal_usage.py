"""Internal Bedrock meter endpoint.

The AWS invocation-log meter Lambda POSTs one extracted record per Bedrock
invocation here. This endpoint is the SINGLE place where pricing, owner
attribution, and ledger depletion happen — the Lambda is a dumb transport, so
swapping the ledger backend (DDB now, Metronome in Phase 3) never touches the
Lambda.

**Auth model.** Like routers/hooks_proxy.py, this router does NOT use
Depends(get_current_user) — the caller is a Lambda inside the VPC, not a Clerk
user. Auth is a bearer `bedrock_meter` service token (service_token.verify_meter).
The token's distinct kind means a leaked meter token cannot authorize container
access (the WS authorizer only trusts kind="paperclip_service").
"""

from __future__ import annotations

import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.billing.bedrock_aip import (
    resolve_owner_and_tier_from_aip_arn,
    resolve_owner_from_identity_arn,
)
from core.billing.bedrock_pricing import UnknownModelError, price_from_tier
from core.billing.ledger_interface import get_ledger
from core.billing.usage_event import UsageEvent
from core.config import settings
from core.observability.metrics import put_metric
from core.services import service_token

# NOTE: owner + pricing tier are resolved together in ONE reverse-table read
# (resolve_owner_and_tier_from_aip_arn, an async DynamoDB GetItem), then the
# tier is priced by price_from_tier (a PURE, synchronous helper) — so the hot
# meter path does exactly one reverse-table read per record. The identity-ARN
# fallback resolver is also async (its own GetItem). `enforce_after_deplete` is
# imported at FUNCTION level inside the endpoint (NOT here) to break an import
# cycle: core/services/bedrock_enforcement.py imports container/ledger services
# that transitively import routers.

logger = logging.getLogger(__name__)
router = APIRouter()


async def require_meter_token(request: Request) -> dict:
    """Auth dependency: validate the bearer bedrock_meter service token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    token = auth[len("Bearer ") :]
    try:
        return service_token.verify_meter(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid_token") from None


class BedrockLogRecord(BaseModel):
    """One extracted Bedrock invocation-log record (the Lambda POSTs this)."""

    request_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)  # the AIP ARN from log `modelId`
    identity_arn: str | None = None  # log `identity.arn` (fallback attribution)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    run_status: str = Field(min_length=1)  # end | error | aborted
    region: str = Field(min_length=1)
    occurred_at: str = Field(min_length=1)  # ISO8601 from log `timestamp`


@router.post("/usage/from-bedrock-log")
async def from_bedrock_log(
    body: BedrockLogRecord,
    _claims: dict = Depends(require_meter_token),
) -> dict:
    """Price + attribute + deplete one Bedrock invocation, exactly once.

    Idempotency is the ledger's job (deplete() is exactly-once on request_id),
    so this endpoint adds none. Returns applied=False on a deduplicated replay,
    an unattributed owner, or an unknown model — all observable via metrics,
    never silently dropped.
    """
    put_metric("meter.received", dimensions={"status": body.run_status})

    # Primary attribution: the per-owner AIP ARN in `modelId`. ONE reverse-table
    # read returns the owner AND the pricing tier together (both live on the same
    # row). Fallback: the assumed-role ARN in `identity.arn` (spec §3.2 mandates
    # BOTH paths — the role ARN attributes records whose AIP-ARN lookup misses,
    # e.g. a stale AIP the registry hasn't indexed yet; that path has no tier, so
    # pricing then raises UnknownModelError below, same as before). Without the
    # fallback the captured identity_arn would be dead code.
    owner_id, tier = await resolve_owner_and_tier_from_aip_arn(body.model_id)
    if owner_id is None and body.identity_arn:
        owner_id = await resolve_owner_from_identity_arn(body.identity_arn)
    if owner_id is None:
        # Unattributed (e.g. GooseTown spillover the filter let through, or an
        # AIP/role we don't own). Do NOT deplete a random owner — skip + observe.
        # model_id (AIP ARN) is a structured log field, NOT a metric dimension
        # (per-owner-per-tier cardinality blows up CloudWatch cost).
        put_metric("meter.unattributed", dimensions={"region": body.region})
        logger.warning("meter: unattributed record model=%s req=%s", body.model_id, body.request_id)
        return {"applied": False, "balance_microcents": 0}

    try:
        raw = price_from_tier(
            tier,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            cache_read_tokens=body.cache_read_tokens,
            cache_write_tokens=body.cache_write_tokens,
        )
    except UnknownModelError:
        # model_id (AIP ARN) is logged as a structured field, NOT passed as a
        # metric dimension value — it is per-owner-per-tier high cardinality.
        put_metric("meter.unknown_model", dimensions={"region": body.region})
        logger.warning("meter: unknown model %s req=%s", body.model_id, body.request_id)
        return {"applied": False, "balance_microcents": 0}

    # 1.4x markup over the raw Bedrock cost, integer math (x14//10) so the
    # microcents ledger never accrues float drift. markup_multiplier=1.4 below is
    # the same factor, recorded for the audit row.
    billed = raw * 14 // 10
    event = UsageEvent(
        owner_id=owner_id,
        request_id=body.request_id,
        model_id=body.model_id,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        cache_read_tokens=body.cache_read_tokens,
        cache_write_tokens=body.cache_write_tokens,
        raw_cost_microcents=raw,
        billed_microcents=billed,
        markup_multiplier=1.4,
        region=body.region,
        run_status=body.run_status,
        occurred_at=body.occurred_at,
        source="bedrock_invocation_log",
    )

    # Cutover seam (GAP 5). `settings.METER_IS_LIVE` is OWNED by Plan 4 (defined
    # in core/config.py there — this plan only CONSUMES it, never redefines it).
    # When False the meter is SHADOW-ONLY: it builds + prices the event and logs
    # the would-be deplete, but does NOT mutate the ledger, so the meter can run
    # alongside the legacy connection_pool biller without double-deducting. Plan
    # 4 flips it to True at cutover (and retires the legacy deduct in the same
    # pass). This is the order-independent toggle the whole cutover relies on.
    if settings.METER_IS_LIVE:
        # Function-level import to avoid an import cycle (the enforcement module
        # imports container/ledger services that transitively import routers).
        from core.services.bedrock_enforcement import enforce_after_deplete

        result = await get_ledger().deplete(event)
        put_metric(
            "meter.depleted",
            dimensions={"applied": str(result.applied), "status": body.run_status},
        )
        # Kill-switch (GAP 4): on a balance that crossed ≤ 0, Plan 4's
        # orchestrator revokes the owner's Bedrock IAM + sets ECS desired=0.
        # Without this call the balance can go negative with nothing stopping
        # further spend — the kill-switch would never fire.
        await enforce_after_deplete(owner_id, result)
        return {"applied": result.applied, "balance_microcents": result.balance_microcents}

    # Shadow / log-only mode: build + price the event, log the would-be deplete,
    # do NOT mutate the ledger and do NOT enforce.
    put_metric("meter.shadow", dimensions={"status": body.run_status})
    logger.info(
        "meter shadow (METER_IS_LIVE=False): would deplete owner=%s billed=%d request=%s",
        owner_id,
        event.billed_microcents,
        event.request_id,
    )
    return {"applied": False, "shadow": True}
