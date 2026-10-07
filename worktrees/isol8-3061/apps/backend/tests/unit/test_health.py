"""
Tests for the health check endpoint.

The /health endpoint is a cheap, local ALB liveness probe:
- HTTP 200 with {"status": "healthy"} once startup has completed
- HTTP 503 with {"status": "starting"} while still booting

It deliberately does NOT call DynamoDB or any upstream — liveness must not
be tied to external availability, or a slow dependency would flap the task
out of the ALB target group. See main.health_check.
"""

import pytest
from httpx import AsyncClient, ASGITransport


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_200_when_started(self):
        """Health endpoint returns 200 once startup has completed."""
        import main

        main._started = True
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_returns_503_before_startup_complete(self):
        """Health endpoint returns 503 while still booting (before startup completes)."""
        import main

        main._started = False
        try:
            transport = ASGITransport(app=main.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/health")

            assert response.status_code == 503
            assert response.json()["status"] == "starting"
        finally:
            main._started = True  # restore so other tests see a started app
