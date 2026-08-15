from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from core.config import settings
from core.services.reliability_pipeline import (
    ReliabilityPaperclipClient,
    ReliabilityPipelineConfig,
    normalize_cloudwatch_event,
    normalize_sentry_event,
    verify_sentry_signature,
)

router = APIRouter()


def _require_shared_secret(authorization: str | None, x_isol8_reliability_secret: str | None) -> None:
    configured = settings.RELIABILITY_WEBHOOK_SECRET
    if not configured:
        raise HTTPException(status_code=404, detail="reliability webhook disabled")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    supplied = supplied or (x_isol8_reliability_secret or "")
    if not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="invalid reliability webhook secret")


def _config() -> ReliabilityPipelineConfig:
    return ReliabilityPipelineConfig(
        paperclip_api_url=settings.RELIABILITY_PAPERCLIP_API_URL or settings.PAPERCLIP_INTERNAL_URL,
        paperclip_api_key=settings.RELIABILITY_PAPERCLIP_API_KEY,
        company_id=settings.RELIABILITY_PAPERCLIP_COMPANY_ID,
        jarvis_agent_id=settings.RELIABILITY_JARVIS_AGENT_ID,
        owner_agent_id=settings.RELIABILITY_OWNER_AGENT_ID,
        escalation_agent_id=settings.RELIABILITY_ESCALATION_AGENT_ID,
        environment=settings.ENVIRONMENT or "unknown",
        dedup_window_hours=settings.RELIABILITY_DEDUP_WINDOW_HOURS,
        parent_issue_id=settings.RELIABILITY_PARENT_ISSUE_ID,
        run_id=settings.RELIABILITY_PAPERCLIP_RUN_ID,
    )


@router.post("/reliability/sentry")
async def sentry_reliability_event(
    request: Request,
    authorization: str | None = Header(default=None),
    x_isol8_reliability_secret: str | None = Header(default=None),
    sentry_hook_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_shared_secret(authorization, x_isol8_reliability_secret)
    raw = await request.body()
    if settings.SENTRY_WEBHOOK_SECRET:
        if not sentry_hook_signature or not verify_sentry_signature(
            raw, sentry_hook_signature, settings.SENTRY_WEBHOOK_SECRET
        ):
            raise HTTPException(status_code=401, detail="invalid sentry signature")
    payload = await request.json()
    signal = normalize_sentry_event(payload)
    return await ReliabilityPaperclipClient(_config()).route(signal)


@router.post("/reliability/cloudwatch")
async def cloudwatch_reliability_event(
    request: Request,
    authorization: str | None = Header(default=None),
    x_isol8_reliability_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_shared_secret(authorization, x_isol8_reliability_secret)
    signal = normalize_cloudwatch_event(await request.json())
    return await ReliabilityPaperclipClient(_config()).route(signal)
