import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, require_platform_admin  # noqa: E402
from routers import ops  # noqa: E402


@pytest.mark.asyncio
async def test_growth_route_wraps_envelope():
    app = FastAPI()
    app.include_router(ops.router, prefix="/api/v1/ops")
    app.dependency_overrides[require_platform_admin] = lambda: AuthContext(
        user_id="u", email="a@isol8.co"
    )  # match the real ctor
    with patch(
        "core.services.ops.growth.compute",
        AsyncMock(return_value=({"x": 1}, "partial", [{"domain": "messaging", "reason": "x"}])),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/ops/growth")
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {"x": 1} and body["source_state"] == "partial" and body["errors"][0]["domain"] == "messaging"
