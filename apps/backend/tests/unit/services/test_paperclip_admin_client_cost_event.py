import json

import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipAdminClient


@pytest.mark.asyncio
async def test_post_cost_event_posts_company_scoped_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ce1"})

    http = httpx.AsyncClient(base_url="http://paperclip.local", transport=httpx.MockTransport(handler))
    try:
        out = await PaperclipAdminClient(http_client=http).post_cost_event(
            session_cookie="c",
            company_id="co1",
            agent_id="ag1",
            model="m",
            cost_cents=3,
            occurred_at="2026-10-01T00:00:00+00:00",
        )
    finally:
        await http.aclose()
    assert seen["url"].endswith("/api/companies/co1/cost-events")
    assert seen["body"] == {
        "agentId": "ag1",
        "provider": "openrouter",
        "model": "m",
        "costCents": 3,
        "occurredAt": "2026-10-01T00:00:00+00:00",
    }
    assert out == {"id": "ce1"}
