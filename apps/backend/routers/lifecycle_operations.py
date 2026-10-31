"""Service-authenticated lifecycle audience and send-ledger contract."""

from datetime import datetime, timezone
import hmac
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import settings
from core.repositories import lifecycle_ledger
from core.services.lifecycle_operations import build_candidates

router = APIRouter()


def _authorize(authorization: str | None = Header(default=None)) -> None:
    secret = settings.PAPERCLIP_EVENTS_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=404, detail="Not found")
    supplied = (authorization or "").encode()
    expected = f"Bearer {secret}".encode()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("/candidates", dependencies=[])
async def candidates(
    audience: Literal["marketing", "transactional"] = "marketing",
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    _authorize(authorization)
    rows, next_cursor = await build_candidates(audience=audience, limit=limit, cursor=cursor)
    return {"candidates": rows, "cursor": next_cursor}


class ClaimRequest(BaseModel):
    recipient: str = Field(min_length=3, max_length=320)
    message: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    eligibility_window: str = Field(min_length=1, max_length=80)
    kind: Literal["lifecycle", "monthly_broadcast"]
    reason: str = Field(min_length=1, max_length=500)
    now: datetime | None = None


@router.post("/ledger/claim")
async def claim_send(body: ClaimRequest, authorization: str | None = Header(default=None)) -> dict:
    _authorize(authorization)
    return await lifecycle_ledger.claim(
        recipient=body.recipient,
        message=body.message,
        eligibility_window=body.eligibility_window,
        kind=body.kind,
        reason=body.reason,
        frequency_cap_hours=72 if body.kind == "lifecycle" else None,
        now=body.now or datetime.now(timezone.utc),
    )


class ResultRequest(BaseModel):
    status: Literal["sent", "failed"]
    provider_message_id: str | None = Field(default=None, max_length=500)
    failure: str | None = Field(default=None, max_length=2000)
    sent_at: datetime | None = None


@router.post("/ledger/{ledger_id}/result")
async def record_send_result(
    ledger_id: str, body: ResultRequest, authorization: str | None = Header(default=None)
) -> dict:
    _authorize(authorization)
    return await lifecycle_ledger.record_result(
        ledger_id=ledger_id,
        status=body.status,
        provider_message_id=body.provider_message_id,
        failure=body.failure,
        sent_at=body.sent_at,
    )
