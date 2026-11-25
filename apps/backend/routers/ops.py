"""Human operator diagnostics for the platform-admin console."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from core.auth import AuthContext, require_platform_admin
from core.services.ops import envelope as env

router = APIRouter()


@router.get("/spark")
async def get_spark(_auth: AuthContext = Depends(require_platform_admin)):
    """Spark product metrics (PostHog HogQL), 5 min cache. Unconfigured PostHog -> unavailable envelope."""
    from core.services.ops import spark_metrics

    data, state, errors = await spark_metrics.compute()
    return env.envelope(data, source_state=state, errors=errors)
