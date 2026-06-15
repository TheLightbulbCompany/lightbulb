"""POST /api/v1/support/tickets: authenticated, validated, delegates to the service."""

from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, get_current_user
from main import app


@pytest.mark.asyncio
async def test_post_ticket_submits(async_client):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_1", email="a@b.com")
    try:
        with patch(
            "routers.support_tickets.support_ticket.submit_ticket",
            new=AsyncMock(return_value="email_1"),
        ) as mock_submit:
            resp = await async_client.post(
                "/api/v1/support/tickets",
                json={"message": "help", "category": "Bug report"},
                headers={"Authorization": "Bearer x"},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    mock_submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_ticket_requires_message(async_client):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_1")
    try:
        resp = await async_client.post(
            "/api/v1/support/tickets",
            json={"message": ""},
            headers={"Authorization": "Bearer x"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 422
