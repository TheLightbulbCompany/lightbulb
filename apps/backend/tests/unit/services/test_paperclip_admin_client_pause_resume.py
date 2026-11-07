# apps/backend/tests/unit/services/test_paperclip_admin_client_pause_resume.py
import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError


def _client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://paperclip.local", transport=transport)
    return PaperclipAdminClient(http_client=http), http


@pytest.mark.asyncio
async def test_pause_agent_posts_to_pause_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"id": "a1", "status": "paused"})

    client, http = _client(handler)
    try:
        out = await client.pause_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/agents/a1/pause")
    assert out["status"] == "paused"


@pytest.mark.asyncio
async def test_resume_agent_posts_to_resume_route():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "a1", "status": "idle"})

    client, http = _client(handler)
    try:
        await client.resume_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert seen["url"].endswith("/api/agents/a1/resume")


@pytest.mark.asyncio
async def test_pause_agent_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client, http = _client(handler)
    try:
        with pytest.raises(PaperclipApiError) as exc:
            await client.pause_agent(session_cookie="c", agent_id="a1")
    finally:
        await http.aclose()
    assert exc.value.status_code == 500


@pytest.fixture(autouse=True)
def _no_registry_mirror(monkeypatch):
    """These pin the Paperclip calls; the registry mirror (always on since the switch retired) has its own suite."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services.paperclip_admin_client import PaperclipAdminClient as _Client

    monkeypatch.setattr(_Client, "_mirror", _AsyncMock())
