"""CORS must expose X-Request-ID so cross-origin browser JS can read it.

Without expose_headers, response.headers.get("X-Request-ID") returns null
cross-origin (it works same-origin, which masks the gap in local dev). The
frontend reads it to attach recent request IDs to support tickets.
"""

import pytest

from core.config import settings


@pytest.mark.asyncio
async def test_cors_exposes_x_request_id(async_client):
    origin = settings.cors_origins_list[0] if settings.cors_origins_list else "http://localhost:3000"
    resp = await async_client.get("/api/v1/openapi.json", headers={"Origin": origin})
    exposed = resp.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in exposed, f"expose-headers={exposed!r}"
