"""Internal door for the openrouter-settlement Lambda: report one settled managed call's cost."""

import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core.config import settings
from core.services import paperclip_cost

router = APIRouter()


class CostBody(BaseModel):
    agentId: str
    generationId: str
    costMicrocents: int
    model: str
    occurredAt: str


@router.post("/cost/{owner_id}", status_code=204)
async def report_cost(owner_id: str, body: CostBody, request: Request) -> Response:
    """Authenticated by the WS internal shared secret (``x-lightbulb-ws-internal``), checked unconditionally."""
    secret = settings.WS_INTERNAL_SECRET
    presented = request.headers.get("x-lightbulb-ws-internal", "")
    if not secret or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")
    await paperclip_cost.record_cost(
        owner_id=owner_id,
        agent_id=body.agentId,
        cost_microcents=body.costMicrocents,
        model=body.model,
        occurred_at=body.occurredAt,
    )
    return Response(status_code=204)
