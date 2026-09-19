"""Unit tests for core.services.owner_posthog.day_traffic.

Mocks httpx.AsyncClient directly via unittest.mock — respx 0.20.2 has a compat issue with newer
httpx (see test_posthog_admin.py), so we patch the client surface instead.

The module is a READER now: the first token source was the container's own MCP OAuth store on EFS,
and a 401 there triggered a refresh whose rotated pair was written back. Both are gone (Hermes
rotates its copy in the profile IN PLACE, so a backend write-back would clobber a fresher token),
so the retained Secrets Manager copy is the only source and there is nothing to write.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import owner_posthog

OWNER = "user_test123"
DAY = "2026-08-30"
QUERY_URL = "https://us.posthog.com/api/projects/@current/query"


def _response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body or {})
    return resp


def _fake_async_client(post_results: list):
    """A patchable httpx.AsyncClient factory whose instance .post returns (or raises) each entry
    of post_results in order."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=post_results)

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield client

    factory = MagicMock(side_effect=_ctx)
    return factory, client


def _retained(token: str | None):
    """The retained Secrets Manager credential, or None for a workspace that never connected."""
    return patch.object(
        owner_posthog.integration_secret_store,
        "get",
        AsyncMock(return_value={"access_token": token} if token else None),
    )


@pytest.mark.asyncio
async def test_happy_path_parses_results():
    factory, client = _fake_async_client([_response(200, {"results": [[42]]})])
    with _retained("tok-live"), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) == {"visitors": 42}

    assert client.post.await_args.args[0] == QUERY_URL
    assert client.post.await_args.kwargs["headers"]["Authorization"] == "Bearer tok-live"
    # The day reaches HogQL only through the interpolation the regex fence guards.
    assert DAY in client.post.await_args.kwargs["json"]["query"]["query"]


@pytest.mark.asyncio
async def test_no_retained_token_returns_none_without_an_http_call():
    """A workspace that never connected PostHog must not cost an outbound request per rollup."""
    factory, client = _fake_async_client([])
    with _retained(None), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
    client.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_a_non_200_returns_none_rather_than_raising(status):
    """Including 401. The reader cannot refresh any more -- the rotated pair lives in the
    workspace's own profile and a write-back from here would clobber it -- so an expired access
    token is a stale tile until a reconnect heals the retained copy, never a 500."""
    factory, _client = _fake_async_client([_response(status)])
    with _retained("tok-stale"), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None


@pytest.mark.asyncio
async def test_a_401_is_not_retried():
    """The refresh-and-retry is gone, so a 401 must cost exactly ONE request. Retrying with the
    same dead token would double every rollup's outbound traffic for nothing."""
    factory, client = _fake_async_client([_response(401), _response(200, {"results": [[7]]})])
    with _retained("tok-stale"), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
    assert client.post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("day", ["2026-8-30", "not-a-day", "2026-08-30'; DROP", ""])
async def test_malformed_day_raises(day):
    """The ONE thing this module raises. ``day`` is interpolated straight into HogQL, so the
    format fence is the injection fence -- it has to be a raise, not a None."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await owner_posthog.day_traffic(OWNER, day)


@pytest.mark.asyncio
async def test_http_error_returns_none():
    factory, _client = _fake_async_client([httpx.ConnectError("boom")])
    with _retained("tok-live"), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"results": []}, {"results": [[]]}, {"results": [["nope"]]}])
async def test_malformed_results_returns_none(body):
    factory, _client = _fake_async_client([_response(200, body)])
    with _retained("tok-live"), patch.object(owner_posthog.httpx, "AsyncClient", factory):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None


@pytest.mark.asyncio
async def test_a_secrets_manager_failure_returns_none():
    """A rollup miss is a stale tile. A transport fault reading the retained copy must not become
    a 500 on the page that renders it."""
    with patch.object(owner_posthog.integration_secret_store, "get", AsyncMock(side_effect=RuntimeError("throttled"))):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
