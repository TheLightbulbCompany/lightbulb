"""POST /api/v1/setup-requests — the public `/request-setup` form.

Unauthenticated, so rate-limited per caller IP with the same in-process bucket
the other public ingress routes use (per instance, see _public_ingress).
"""

from fastapi import APIRouter, HTTPException, Request

from core.services import setup_requests
from core.services.rate_bucket import RateBucket
from routers._public_ingress import _extract_caller_ip
from schemas.setup_request import SetupRequest

router = APIRouter()

_RATE = RateBucket(5)


@router.post("", status_code=201, operation_id="create_setup_request")
async def create_setup_request(body: SetupRequest, request: Request) -> dict:
    if not _RATE.allow(_extract_caller_ip(request)):
        raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "60"})
    return await setup_requests.submit(body)
