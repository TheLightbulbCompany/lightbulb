"""A text/html attachment never renders on the API origin: it downloads, and carries a sandbox CSP."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from routers import messaging as messaging_router


def _app(monkeypatch, content_type: str, file_name: str) -> FastAPI:
    app = FastAPI()
    app.include_router(messaging_router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_1", org_id="org_1", email="u@example.com"
    )
    meta = ({"ownerId": "org_1"}, None)
    attachment = SimpleNamespace(content_type=content_type, file_name=file_name)
    monkeypatch.setattr(messaging_router.service, "require_room_member", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging_router, "run_in_thread", AsyncMock(return_value=(attachment, b"<html>hi</html>")))
    monkeypatch.setattr(messaging_router, "resolve_owner_id", lambda auth: "org_1")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type,file_name,disposition,csp",
    [
        ("text/html", "page.html", "attachment", "sandbox"),
        ("application/zip", "bundle.zip", "attachment", None),
        ("application/pdf", "report.pdf", "inline", None),
    ],
)
async def test_html_downloads_with_a_sandbox_csp(monkeypatch, content_type, file_name, disposition, csp):
    async with AsyncClient(
        transport=ASGITransport(app=_app(monkeypatch, content_type, file_name)), base_url="http://t"
    ) as client:
        response = await client.get("/api/v1/messaging/rooms/room-1/attachments/att-1")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(f"{disposition}; filename*=UTF-8''{file_name}")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("content-security-policy") == csp


@pytest.mark.asyncio
async def test_head_returns_only_the_first_bytes_by_a_ranged_read(monkeypatch):
    """`?head=N` (a doc tile's preview) reads N bytes from storage with a ranged get, never the whole file."""
    from core.services import messaging as service

    meta_json = (
        '{"id": "00000000-0000-0000-0000-000000000001", "fileName": "menu.md", '
        '"contentType": "text/markdown", "size": 100000}'
    )
    objects = {"meta": meta_json.encode(), "bytes": b"# Menu\n" + b"x" * 99993}
    ranged: list[tuple[str, int]] = []
    monkeypatch.setattr(service, "attachment_key", lambda o, r, name: "meta" if name.endswith(".json") else "bytes")
    monkeypatch.setattr(
        service.hermes_profiles,
        "get_owner_object",
        lambda key: objects[key] if key == "meta" else pytest.fail("full read"),
    )
    monkeypatch.setattr(
        service.hermes_profiles, "get_owner_object_head", lambda key, n: ranged.append((key, n)) or objects[key][:n]
    )

    attachment, data = service.load_attachment_head("org_1", "room-1", "00000000-0000-0000-0000-000000000001", 4096)
    assert (attachment.file_name, data[:7], len(data), ranged) == ("menu.md", b"# Menu\n", 4096, [("bytes", 4096)])
    # Capped at 16 KB whatever is asked.
    assert (
        len(service.load_attachment_head("org_1", "room-1", "00000000-0000-0000-0000-000000000001", 10**9)[1])
        == 16 * 1024
    )

    app = FastAPI()
    app.include_router(messaging_router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_1", org_id="org_1", email="u@example.com"
    )
    monkeypatch.setattr(
        messaging_router.service, "require_room_member", AsyncMock(return_value=({"ownerId": "org_1"}, None))
    )
    monkeypatch.setattr(messaging_router, "resolve_owner_id", lambda auth: "org_1")

    async def inline(fn, *args):
        return fn(*args)

    monkeypatch.setattr(messaging_router, "run_in_thread", inline)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        ok = await client.get(
            "/api/v1/messaging/rooms/room-1/attachments/00000000-0000-0000-0000-000000000001?head=4096"
        )
        too_big = await client.get(
            "/api/v1/messaging/rooms/room-1/attachments/00000000-0000-0000-0000-000000000001?head=20000"
        )
    assert ok.status_code == 200 and len(ok.content) == 4096 and ok.headers["content-type"].startswith("text/markdown")
    assert too_big.status_code == 422
