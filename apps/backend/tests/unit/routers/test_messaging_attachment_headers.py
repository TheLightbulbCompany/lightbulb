"""A text/html attachment never renders on the API origin: it downloads, and carries a sandbox CSP."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from routers import messaging as messaging_router


def _app(content_type: str, file_name: str) -> FastAPI:
    app = FastAPI()
    app.include_router(messaging_router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_1", org_id="org_1", email="u@example.com"
    )
    meta = ({"ownerId": "org_1"}, None)
    attachment = SimpleNamespace(content_type=content_type, file_name=file_name)
    patch.object(messaging_router.service, "require_room_member", AsyncMock(return_value=meta)).start()
    patch.object(messaging_router, "run_in_thread", AsyncMock(return_value=(attachment, b"<html>hi</html>"))).start()
    patch.object(messaging_router, "resolve_owner_id", lambda auth: "org_1").start()
    return app


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,file_name,disposition,csp",
    [
        ("text/html", "page.html", "attachment", "sandbox"),
        ("application/zip", "bundle.zip", "attachment", None),
        ("application/pdf", "report.pdf", "inline", None),
    ],
)
async def test_html_downloads_with_a_sandbox_csp(content_type, file_name, disposition, csp):
    async with AsyncClient(transport=ASGITransport(app=_app(content_type, file_name)), base_url="http://t") as client:
        response = await client.get("/api/v1/messaging/rooms/room-1/attachments/att-1")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(f"{disposition}; filename*=UTF-8''{file_name}")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("content-security-policy") == csp
