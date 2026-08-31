"""Unit tests for core.services.owner_posthog.day_traffic.

Mocks httpx.AsyncClient directly via unittest.mock — respx 0.20.2 has a compat
issue with newer httpx (see test_posthog_admin.py), so we patch the client
surface instead. The EFS OAuth store is a real tmp_path file so the
rotation-aware refresh write-back is exercised for real.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import owner_posthog

OWNER = "user_test123"
DAY = "2026-08-30"
TOKEN_URL = "https://oauth.posthog.com/oauth/token/"
QUERY_URL = "https://us.posthog.com/api/projects/@current/query"


def _response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body or {})
    return resp


def _fake_async_client(post_results: list):
    """A patchable httpx.AsyncClient factory whose instance .post returns (or
    raises) each entry of post_results in order."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=post_results)

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield client

    factory = MagicMock(side_effect=_ctx)
    return factory, client


def _write_store(tmp_path, tokens: dict) -> object:
    path = tmp_path / "posthog-abc123.json"
    path.write_text(json.dumps({"clientInformation": {"client_id": "cid_1"}, "tokens": tokens}))
    return path


async def test_happy_path_parses_results(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_1", "token_type": "Bearer"})
    factory, client = _fake_async_client([_response(200, {"results": [[42]]})])
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) == {"visitors": 42}

    args, kwargs = client.post.call_args
    assert args[0] == QUERY_URL
    assert kwargs["headers"] == {"Authorization": "Bearer at_1"}
    assert kwargs["json"]["query"]["kind"] == "HogQLQuery"
    assert f"toDate('{DAY}')" in kwargs["json"]["query"]["query"]


async def test_no_token_anywhere_returns_none_without_http_call(tmp_path):
    factory, client = _fake_async_client([])
    with (
        patch.object(owner_posthog, "_store_file", return_value=tmp_path / "missing.json"),
        patch.object(owner_posthog.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
    client.post.assert_not_called()


async def test_429_returns_none(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_1"})
    factory, _ = _fake_async_client([_response(429)])
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None


@pytest.mark.parametrize("day", ["2026-8-30", "20260830", "2026-08-30'; DROP", "yesterday"])
async def test_malformed_day_raises(day):
    with pytest.raises(ValueError):
        await owner_posthog.day_traffic(OWNER, day)


async def test_401_refreshes_persists_rotated_pair_and_retries(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_old", "refresh_token": "rt_old", "token_type": "Bearer"})
    factory, client = _fake_async_client(
        [
            _response(401),
            _response(200, {"access_token": "at_new", "refresh_token": "rt_new"}),
            _response(200, {"results": [[7]]}),
        ]
    )
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) == {"visitors": 7}

    # Refresh POST: form-encoded public-client grant against the registry's token_url.
    args, kwargs = client.post.call_args_list[1]
    assert args[0] == TOKEN_URL
    assert kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt_old",
        "client_id": "cid_1",
    }

    # Retry used the fresh access token.
    _, kwargs = client.post.call_args_list[2]
    assert kwargs["headers"] == {"Authorization": "Bearer at_new"}

    # BOTH rotated tokens persisted back to the same store file.
    store = json.loads(path.read_text())
    assert store["tokens"]["access_token"] == "at_new"
    assert store["tokens"]["refresh_token"] == "rt_new"
    assert store["clientInformation"] == {"client_id": "cid_1"}


async def test_refresh_invalid_grant_returns_none(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_old", "refresh_token": "rt_dead"})
    factory, client = _fake_async_client([_response(401), _response(400, {"error": "invalid_grant"})])
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
    assert client.post.call_count == 2
    # Store untouched on a failed refresh.
    assert json.loads(path.read_text())["tokens"]["access_token"] == "at_old"


async def test_efs_miss_falls_back_to_secrets_manager(tmp_path):
    factory, client = _fake_async_client([_response(200, {"results": [[3]]})])
    with (
        patch.object(owner_posthog, "_store_file", return_value=tmp_path / "missing.json"),
        patch.object(
            owner_posthog.integration_secret_store,
            "get",
            AsyncMock(return_value={"access_token": "at_sm"}),
        ),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) == {"visitors": 3}
    _, kwargs = client.post.call_args
    assert kwargs["headers"] == {"Authorization": "Bearer at_sm"}


async def test_http_error_returns_none(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_1"})
    factory, _ = _fake_async_client([httpx.ConnectTimeout("boom")])
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None


async def test_malformed_results_returns_none(tmp_path):
    path = _write_store(tmp_path, {"access_token": "at_1"})
    factory, _ = _fake_async_client([_response(200, {"results": []})])
    with (
        patch.object(owner_posthog, "_store_file", return_value=path),
        patch.object(owner_posthog.httpx, "AsyncClient", factory),
    ):
        assert await owner_posthog.day_traffic(OWNER, DAY) is None
