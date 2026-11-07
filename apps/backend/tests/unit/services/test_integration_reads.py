"""Bounded integration reads for a voice seat: one provider each, HTTP mocked."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.services import integration_reads as ir
from core.services.credentials import access_token

_RealClient = httpx.AsyncClient

WS = "org_1"


class _Route:
    def __init__(self):
        self.calls, self.handler = [], lambda r: httpx.Response(200, json={})

    def respond(self, status_code=200, json=None):
        self.handler = lambda r: httpx.Response(status_code, json=json)
        return self

    def mock(self, side_effect):
        self.handler = side_effect
        return self


class _Http:
    """A tiny respx: routes by method and URL prefix, the newest first."""

    def __init__(self):
        self.routes = []

    def _add(self, method, url__startswith):
        route = _Route()
        self.routes.insert(0, (method, url__startswith, route))
        return route

    def get(self, url__startswith):
        return self._add("GET", url__startswith)

    def post(self, url__startswith):
        return self._add("POST", url__startswith)

    async def __call__(self, request):
        for method, prefix, route in self.routes:
            if request.method == method and str(request.url).startswith(prefix):
                route.calls.append(type("Call", (), {"request": request}))
                out = route.handler(request)
                return await out if asyncio.iscoroutine(out) else out
        raise AssertionError(f"not mocked: {request.method} {request.url}")


@pytest.fixture
def env():
    rows = [{"provider": p, "status": "connected"} for p in ("google", "github", "posthog", "notion", "slack")]
    http = _Http()
    secrets = {"github": {"access_token": "gh"}, "posthog": {"access_token": "ph"}}

    async def token(ws, key):
        if key == "notion":
            return "nt"
        raise ValueError("actor-refreshed")

    with (
        patch.object(ir.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(ir.integration_secret_store, "get", AsyncMock(side_effect=lambda ws, k: secrets.get(k))),
        patch.object(ir.access_token, "get", AsyncMock(side_effect=token)),
        patch.object(ir.httpx, "AsyncClient", lambda **kw: _RealClient(transport=httpx.MockTransport(http), **kw)),
    ):
        yield {"http": http, "secrets": secrets, "rows": rows}


async def test_connected_lists_readable_names(env):
    names = ["knowledge", "gmail", "calendar", "drive", "github", "posthog", "notion", "slack"]
    assert await ir.connected(WS) == names


async def test_unconnected_or_unknown_provider(env):
    assert await ir.read(WS, "linear", "bugs") == "linear is not connected."
    assert await ir.read(WS, "salesforce", "x") == "salesforce is not connected."


async def test_google_refresh_failure_says_ask_again_a_missing_grant_says_not_connected(env):
    ir.access_token.get.side_effect = ValueError("google: refresh failed (HTTPStatusError)")
    assert await ir.read(WS, "gmail", "invoice") == ir.GOOGLE_RETRY
    ir.access_token.get.side_effect = ValueError("google: no exportable grant in the workspace keyring")
    assert await ir.read(WS, "gmail", "invoice") == ir.GOOGLE_OFF


async def test_a_slow_token_fetch_is_not_cut_by_the_read_budget(env):
    warm = []

    async def cold(ws, key):  # like google_grant: slow once (longer than READ_S below), then cached
        if not warm:
            await asyncio.sleep(0.2)
            warm.append(1)
        return "g"

    ir.access_token.get.side_effect = cold
    env["http"].get(url__startswith="https://www.googleapis.com/calendar").respond(
        json={"items": [{"summary": "Standup", "start": {"dateTime": "2026-11-09T09:00"}}]}
    )
    with patch.object(ir, "READ_S", 0.1):
        assert await ir.read(WS, "calendar", "") == "Standup at 2026-11-09T09:00"
        warm.clear()
        with patch.object(ir, "TOKEN_S", 0.05):
            assert await ir.read(WS, "calendar", "") == ir.GOOGLE_RETRY


async def test_warm_google_never_raises(env):
    await ir.warm_google(WS)  # the mocked fetch raises ValueError
    ir.access_token.get.assert_awaited_with(WS, "google")


async def test_gmail_with_a_backend_token(env):
    ir.access_token.get.side_effect = None
    ir.access_token.get.return_value = "g"
    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    env["http"].get(url__startswith=base).respond(json={"messages": [{"id": "m1"}]})
    env["http"].get(url__startswith=f"{base}/m1").respond(
        json={
            "snippet": "Due Friday",
            "payload": {"headers": [{"name": "Subject", "value": "Invoice 42"}, {"name": "From", "value": "Acme"}]},
        }  # fmt: skip
    )
    assert await ir.read(WS, "gmail", "invoice") == "Invoice 42 — from Acme: Due Friday"


async def test_github_issue_search(env):
    route = (
        env["http"]
        .get(url__startswith="https://api.github.com/search/issues")
        .respond(
            json={
                "items": [
                    {
                        "title": "Login broken",
                        "state": "open",
                        "number": 7,
                        "repository_url": "https://api.github.com/repos/acme/web",
                    }
                ]
            }  # fmt: skip
        )
    )
    assert await ir.read(WS, "GitHub", "login") == "Login broken (open, acme/web #7)"
    assert route.calls[0].request.headers["Authorization"] == "Bearer gh"


async def test_posthog_counts_and_fences_the_event(env):
    route = (
        env["http"]
        .post(url__startswith="https://us.posthog.com/api/projects/@current/query")
        .respond(json={"results": [[120, 40]]})
    )
    assert await ir.read(WS, "posthog", "signup") == "signup: 120 events from 40 people in the last 7 days"
    await ir.read(WS, "posthog", "x' OR 1=1 --")
    assert "'$pageview'" in route.calls[1].request.content.decode()  # an unsafe name falls back


async def test_notion_over_hosted_mcp(env):
    url = "https://mcp.notion.com/mcp"

    def rpc(request):
        body = request.content and __import__("json").loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"result": {}}, headers={"mcp-session-id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Mcp-Session-Id"] == "s1"
        if method == "tools/list":
            return httpx.Response(
                200, json={"result": {"tools": [{"name": "notion-fetch"}, {"name": "notion-search"}]}}
            )
        assert body["params"] == {"name": "notion-search", "arguments": {"query": "holiday hours"}}
        sse = 'event: message\ndata: {"result": {"content": [{"type": "text", "text": "Holiday hours page"}]}}\n\n'
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    env["http"].post(url__startswith=url).mock(side_effect=rpc)
    assert await ir.read(WS, "notion", "holiday hours") == "Holiday hours page"


async def test_stripe_with_the_business_key(env):
    env["secrets"]["company_stripe"] = {"api_key": "rk_test_1"}
    env["http"].get(url__startswith="https://api.stripe.com/v1/balance").respond(
        json={"available": [{"amount": 12345, "currency": "usd"}]}
    )
    env["http"].get(url__startswith="https://api.stripe.com/v1/charges").respond(
        json={"data": [{"amount": 4500, "currency": "usd", "status": "succeeded", "description": "Gel manicure"}]}
    )
    out = await ir.read(WS, "stripe", "")
    assert out.splitlines() == ["Available balance: 123.45 USD", "45.00 USD succeeded — Gel manicure"]


async def test_slow_reconnect_long_and_failing_reads_are_sentences(env):
    async def slow(*_a, **_k):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    env["http"].get(url__startswith="https://api.github.com/search/issues").mock(side_effect=slow)
    with patch.object(ir, "READ_S", 0.05):
        assert "didn't answer in time" in await ir.read(WS, "github", "x")
    env["http"].get(url__startswith="https://api.github.com/search/issues").respond(
        json={"items": [{"title": "t" * 900, "number": i} for i in range(5)]}
    )
    assert len(await ir.read(WS, "github", "x")) == ir.ANSWER_CHARS
    env["http"].get(url__startswith="https://api.github.com/search/issues").respond(500)
    assert await ir.read(WS, "github", "x") == "I couldn't read github just now."
    ir.access_token.get.side_effect = access_token.ReconnectRequired("notion")
    assert "reconnecting" in await ir.read(WS, "notion", "x")
    assert "isn't available" in await ir.read(WS, "slack", "x")


def _hosted(tools, want_tool, text):
    def rpc(request):
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"result": {"tools": [{"name": t} for t in tools]}})
        assert request.headers["Authorization"] == "Bearer at"
        assert body["params"] == {"name": want_tool, "arguments": {"query": "Acme"}}
        return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": text}]}})

    return rpc


async def test_mcp_fallback_for_unlisted_provider(env):
    """attio has no hand-written reader: its providers.yaml mcp_remote url is read with its own search tool."""
    env["rows"].append({"provider": "attio", "status": "connected"})
    env["secrets"]["attio"] = {"access_token": "at"}
    assert "attio" in await ir.connected(WS)
    route = env["http"].post(url__startswith="https://mcp.attio.com/mcp")
    route.mock(side_effect=_hosted(["get-record", "search-records"], "search-records", "Acme Corp, deal open"))
    assert await ir.read(WS, "attio", "Acme") == "Acme Corp, deal open"
    route.mock(side_effect=_hosted(["get-record", "list-records"], "list-records", "Acme Corp"))  # no search: list
    assert await ir.read(WS, "attio", "Acme") == "Acme Corp"


async def test_no_reader_hands_to_work(env):
    """x is connected but has neither a reader nor a hosted MCP: the voice says "I'll get that" and starts work."""
    env["rows"].append({"provider": "x", "status": "connected"})
    assert "x" in await ir.connected(WS)
    assert await ir.read(WS, "x", "mentions this week") == {"start_work": True, "say": "I'll get that"}


async def test_knowledge_read(env):
    """The Agent page's services, hours and Q&A (the phone's _facts) plus its instructions."""
    from routers import voice_phone

    page = {
        "services": [{"name": "Gel manicure", "price": "$45", "minutes": 60}],
        "knowledge": [{"q": "Parking?", "a": "Free out back"}],
        "instructions": "Mention the loyalty card.",
    }
    with patch.object(voice_phone.messaging_repo, "get_voice_agent", AsyncMock(return_value=page)):
        owner = await ir.read(WS, "knowledge", "parking", instructions=True)
        meet = await ir.read(WS, "knowledge", "parking")
    assert "Gel manicure $45 (60 min)" in owner and "Q: Parking? A: Free out back" in owner
    assert owner.endswith("Instructions: Mention the loyalty card.")
    assert "Q: Parking? A: Free out back" in meet and "Instructions" not in meet  # guests hear a Meet


async def test_knowledge_keeps_a_long_page_whole(env):
    """Its own budget, the phone's: a late Q&A answer and the instructions survive a page over ANSWER_CHARS."""
    from routers import voice_phone

    page = {
        "knowledge": [{"q": f"Question {i}?", "a": "x" * 80} for i in range(30)] + [{"q": "Wifi?", "a": "salon2026"}],
        "instructions": "Mention the loyalty card. " + "y" * 3000,
    }
    with patch.object(voice_phone.messaging_repo, "get_voice_agent", AsyncMock(return_value=page)):
        out = await ir.read(WS, "knowledge", "", instructions=True)
    assert len(out) > ir.ANSWER_CHARS + 3000
    assert "Q: Wifi? A: salon2026" in out and "Instructions: Mention the loyalty card." in out


async def test_knowledge_without_an_agent_page_states_no_default_hours(env):
    from routers import voice_phone

    with patch.object(voice_phone.messaging_repo, "get_voice_agent", AsyncMock(return_value=None)):
        assert await ir.read(WS, "knowledge", "hours") == "Hours aren't set yet."


async def test_an_empty_web_query_makes_no_paid_call(env):
    env["rows"].append({"provider": "parallel", "status": "connected"})
    env["secrets"]["parallel"] = {"token": "pk"}
    assert await ir.read(WS, "web", "   ") == "What should I look up?"  # _Http raises on any unmocked request


async def test_mcp_reads_get_the_longer_bound(env):
    """Four round trips to a hosted MCP: MCP_READ_S, not READ_S; a non-MCP read keeps READ_S."""
    env["rows"].append({"provider": "attio", "status": "connected"})
    env["secrets"]["attio"] = {"access_token": "at"}
    hosted = _hosted(["search-records"], "search-records", "Acme Corp")

    async def slow(request):
        await asyncio.sleep(0.2)
        return hosted(request)

    env["http"].post(url__startswith="https://mcp.attio.com/mcp").mock(side_effect=slow)
    env["http"].get(url__startswith="https://api.github.com/search/issues").mock(side_effect=slow)
    with patch.object(ir, "READ_S", 0.1), patch.object(ir, "MCP_READ_S", 5):
        assert await ir.read(WS, "attio", "Acme") == "Acme Corp"
        assert "didn't answer in time" in await ir.read(WS, "github", "Acme")
    with patch.object(ir, "MCP_READ_S", 0.1):
        assert "didn't answer in time" in await ir.read(WS, "attio", "Acme")


async def test_web_read_uses_parallel(env):
    """web is the workspace's own Parallel key (providers.yaml `parallel`), on Parallel's documented Search API."""
    assert "web" not in await ir.connected(WS)
    env["rows"].append({"provider": "parallel", "status": "connected"})
    env["secrets"]["parallel"] = {"token": "pk"}
    route = (
        env["http"]
        .post(url__startswith="https://api.parallel.ai/v1/search")
        .respond(
            json={
                "search_id": "s",
                "results": [{"url": "https://ex.com/a", "title": "Rates 2026", "excerpts": ["Rates rose."]}],
            }
        )
    )
    assert "web" in await ir.connected(WS)
    assert await ir.read(WS, "web", "fed rate today") == "Rates 2026 (https://ex.com/a): Rates rose."
    req = route.calls[0].request
    assert req.headers["x-api-key"] == "pk"
    body = json.loads(req.content)
    assert body["search_queries"] == ["fed rate today"] and body["mode"] == "fast"
