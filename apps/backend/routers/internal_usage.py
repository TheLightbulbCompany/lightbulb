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

from core.observability.metrics import put_metric
from core.services import service_token

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
    """Price + attribute + deplete one Bedrock invocation. Filled in Task 6."""
    put_metric("meter.received", dimensions={"status": body.run_status})
    return {"applied": False, "balance_microcents": 0}
