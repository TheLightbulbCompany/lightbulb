"""Tests for clerk_admin (Phase B v1 read-side surface).

Same direct-httpx-mock approach as test_posthog_admin.py — respx 0.20.2
+ newer httpx have a route-matching incompat.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _make_response(status_code: int, json_body=None):
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body if json_body is not None else [])
    return response


@asynccontextmanager
async def _fake_client(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        client.get = AsyncMock(side_effect=response_or_exc)
    else:
        client.get = AsyncMock(return_value=response_or_exc)
    yield client


@asynccontextmanager
async def _fake_post_client(client):
    """Like `_fake_client`, but yields a caller-built client so the test
    can inspect `client.post.call_args` afterwards (`_fake_client` builds
    a fresh mock per call, which is fine for GET-only assertions but hides
    the call from POST tests that need to check the request body)."""
    yield client


@pytest.mark.asyncio
async def test_list_users_stubs_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services.clerk_admin import list_users

    result = await list_users(query="x", limit=10, offset=0)
    assert result["stubbed"] is True
    assert result["users"] == []
    assert result["next_offset"] is None


@pytest.mark.asyncio
async def test_list_users_happy_path(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    body = [{"id": "user_a"}, {"id": "user_b"}]
    response = _make_response(200, json_body=body)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users(query="", limit=2, offset=0)

    assert len(result["users"]) == 2
    assert result["users"][0]["id"] == "user_a"
    assert result["next_offset"] == 2  # got `limit` results → there may be more
    assert result["stubbed"] is False


@pytest.mark.asyncio
async def test_list_users_no_more_pages_when_under_limit(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(200, json_body=[{"id": "user_a"}])
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users(limit=10)

    assert result["next_offset"] is None  # < limit → end of list


@pytest.mark.asyncio
async def test_list_users_timeout_returns_error(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(httpx.TimeoutException("slow")),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users()

    assert result["error"] == "timeout"
    assert result["users"] == []


@pytest.mark.asyncio
async def test_list_users_5xx_returns_error(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(503, json_body={})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import list_users

        result = await list_users()

    assert result["error"] == "http_503"


@pytest.mark.asyncio
async def test_get_user_returns_none_on_404(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(404, json_body={})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import get_user

        result = await get_user("user_missing")

    assert result is None


@pytest.mark.asyncio
async def test_get_user_returns_none_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services.clerk_admin import get_user

    assert await get_user("any") is None


@pytest.mark.asyncio
async def test_get_user_returns_payload_on_200(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(200, json_body={"id": "user_a", "first_name": "Alice"})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        from core.services.clerk_admin import get_user

        result = await get_user("user_a")

    assert result["id"] == "user_a"
    assert result["first_name"] == "Alice"


# --- list_organization_members -------------------------------------------------


def _membership(user_id: str, first: str | None, last: str | None, email: str) -> dict:
    return {
        "id": f"orgmem_{user_id}",
        "role": "org:member",
        "public_user_data": {
            "user_id": user_id,
            "first_name": first,
            "last_name": last,
            "identifier": email,
            "image_url": f"https://img/{user_id}",
        },
    }


@pytest.mark.asyncio
async def test_list_organization_members_flattens_public_user_data_and_pages(monkeypatch):
    from core.services import clerk_admin

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    monkeypatch.setattr(clerk_admin, "_ORG_MEMBERS_PAGE", 1)
    pages = [
        _make_response(200, {"data": [_membership("u1", "Ada", "Lovelace", "ada@x.co")], "total_count": 2}),
        _make_response(200, {"data": [_membership("u2", None, None, "leo@x.co")], "total_count": 2}),
        _make_response(200, {"data": [], "total_count": 2}),
    ]

    @asynccontextmanager
    async def fake_client(*a, **k):
        client = MagicMock()
        client.get = AsyncMock(side_effect=pages)
        yield client

    with patch("core.services.clerk_admin.httpx.AsyncClient", new=fake_client):
        members = await clerk_admin.list_organization_members("org_1")

    assert members == [
        {
            "user_id": "u1",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@x.co",
            "image_url": "https://img/u1",
            "role": "org:member",
        },
        {
            "user_id": "u2",
            "first_name": "",
            "last_name": "",
            "email": "leo@x.co",
            "image_url": "https://img/u2",
            "role": "org:member",
        },
    ]


@pytest.mark.asyncio
async def test_list_organization_members_never_returns_a_partial_roster(monkeypatch):
    """A 5xx on any page → [] (not the rows gathered so far): a silently short
    roster hides teammates, which is the very bug the picker had."""
    from core.services import clerk_admin

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    monkeypatch.setattr(clerk_admin, "_ORG_MEMBERS_PAGE", 1)
    pages = [
        _make_response(200, {"data": [_membership("u1", "Ada", "Lovelace", "ada@x.co")], "total_count": 2}),
        _make_response(503, {"errors": []}),
    ]

    @asynccontextmanager
    async def fake_client(*a, **k):
        client = MagicMock()
        client.get = AsyncMock(side_effect=pages)
        yield client

    with patch("core.services.clerk_admin.httpx.AsyncClient", new=fake_client):
        assert await clerk_admin.list_organization_members("org_1") == []


@pytest.mark.asyncio
async def test_list_organization_members_timeout_returns_empty(monkeypatch):
    from core.services import clerk_admin

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(httpx.TimeoutException("slow")),
    ):
        assert await clerk_admin.list_organization_members("org_1") == []


@pytest.mark.asyncio
async def test_list_organization_members_stubs_when_no_secret_key(monkeypatch):
    from core.services import clerk_admin

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    assert await clerk_admin.list_organization_members("org_1") == []


@pytest.mark.asyncio
async def test_create_organization_returns_none_when_no_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services.clerk_admin import create_organization

    result = await create_organization(name="Acme", slug="acme", created_by="user_1")

    assert result is None


@pytest.mark.asyncio
async def test_create_organization_happy_path_body_and_headers(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(201, json_body={"id": "org_new", "slug": "acme", "name": "Acme"})
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_post_client(client),
    ):
        from core.services.clerk_admin import create_organization

        result = await create_organization(name="Acme", slug="acme", created_by="user_1")

    assert result == {"id": "org_new", "slug": "acme", "name": "Acme"}
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.clerk.com/v1/organizations"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_xyz"
    assert kwargs["json"] == {"name": "Acme", "slug": "acme", "created_by": "user_1"}


@pytest.mark.asyncio
async def test_create_organization_slug_taken_raises(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(
        422,
        json_body={"errors": [{"code": "form_identifier_exists", "message": "that slug is taken"}]},
    )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_post_client(client),
    ):
        from core.services.clerk_admin import SlugTakenError, create_organization

        with pytest.raises(SlugTakenError):
            await create_organization(name="Acme", slug="acme", created_by="user_1")


@pytest.mark.asyncio
async def test_create_organization_5xx_returns_none(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(503, json_body={})
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_post_client(client),
    ):
        from core.services.clerk_admin import create_organization

        result = await create_organization(name="Acme", slug="acme", created_by="user_1")

    assert result is None


@pytest.mark.asyncio
async def test_create_organization_timeout_returns_none(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_post_client(client),
    ):
        from core.services.clerk_admin import create_organization

        result = await create_organization(name="Acme", slug="acme", created_by="user_1")

    assert result is None


@pytest.mark.asyncio
async def test_create_organization_unrelated_422_returns_none(monkeypatch):
    """A 422 that isn't a slug collision is a plain failure, not SlugTakenError."""
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    response = _make_response(422, json_body={"errors": [{"code": "form_param_format_invalid"}]})
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_post_client(client),
    ):
        from core.services.clerk_admin import create_organization

        result = await create_organization(name="Acme", slug="acme", created_by="user_1")

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expect_raise", [(404, False), (503, True)])
async def test_list_organization_members_strict_404_is_empty_other_errors_raise(monkeypatch, status, expect_raise):
    """A re-run of the workspace wipe meets a Clerk org the first run deleted:
    404 means "no members", while any other error still fails closed."""
    from core.services import clerk_admin

    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")

    @asynccontextmanager
    async def fake_client(*a, **k):
        client = MagicMock()
        client.get = AsyncMock(return_value=_make_response(status, {"errors": []}))
        yield client

    with patch("core.services.clerk_admin.httpx.AsyncClient", new=fake_client):
        if expect_raise:
            with pytest.raises(RuntimeError):
                await clerk_admin.list_organization_members("org_gone", strict=True)
        else:
            assert await clerk_admin.list_organization_members("org_gone", strict=True) == []
