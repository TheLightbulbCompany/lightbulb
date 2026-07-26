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


@pytest.mark.asyncio
async def test_member_count_reads_members_count(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"members_count": 7}))
    assert await clerk_admin.get_organization_member_count("org_1") == 7


@pytest.mark.asyncio
async def test_member_count_falls_back_to_memberships_total_count(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    # get_organization returns no usable members_count -> fall back to the
    # memberships list endpoint's total_count.
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={}))
    response = _make_response(200, json_body={"data": [], "total_count": 3})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        assert await clerk_admin.get_organization_member_count("org_1") == 3


@pytest.mark.asyncio
async def test_member_count_stubs_to_zero_without_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services import clerk_admin

    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value=None))
    assert await clerk_admin.get_organization_member_count("org_1") == 0


@pytest.mark.asyncio
async def test_pending_invitation_count_stubs_to_zero_without_secret_key(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", None)
    from core.services import clerk_admin

    assert await clerk_admin._pending_invitation_count("org_1") == 0


@pytest.mark.asyncio
async def test_pending_invitation_count_sends_status_pending_filter(monkeypatch):
    """Nothing else proves the status=pending filter (and the page-size limit
    it's paired with) is actually sent — without status=pending,
    revoked/expired invitations would inflate the seat count; a silently
    drifted limit would change the truncation threshold without anyone
    noticing."""
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    response = _make_response(200, json_body={"data": [{"id": "orginv_1"}], "total_count": 1})
    captured_params = {}

    @asynccontextmanager
    async def _capturing_client(response_to_return):
        client = MagicMock()

        async def _get(url, headers=None, params=None):
            captured_params.update(params or {})
            return response_to_return

        client.get = _get
        yield client

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _capturing_client(response),
    ):
        result = await clerk_admin._pending_invitation_count("org_1")

    assert result == 1
    assert captured_params.get("status") == "pending"
    assert captured_params.get("limit") == clerk_admin._PENDING_INVITATION_PAGE_LIMIT


@pytest.mark.asyncio
async def test_pending_invitation_count_counts_data_length_not_total_count(monkeypatch):
    """Reads len(data), NOT the envelope's total_count — nothing establishes
    that Clerk's total_count actually respects the status filter, so trusting
    it could silently count accepted invitations too."""
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    # total_count deliberately lies (claims 99) to prove it's ignored.
    response = _make_response(200, json_body={"data": [{"id": "orginv_1"}, {"id": "orginv_2"}], "total_count": 99})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        assert await clerk_admin._pending_invitation_count("org_1") == 2


@pytest.mark.asyncio
async def test_pending_invitation_count_returns_none_on_4xx(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    response = _make_response(429, json_body={})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        assert await clerk_admin._pending_invitation_count("org_1") is None


@pytest.mark.asyncio
async def test_pending_invitation_count_returns_none_on_network_error(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(httpx.TimeoutException("slow")),
    ):
        assert await clerk_admin._pending_invitation_count("org_1") is None


@pytest.mark.asyncio
async def test_pending_invitation_count_returns_none_on_malformed_json(monkeypatch):
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json = MagicMock(side_effect=ValueError("bad json"))
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        assert await clerk_admin._pending_invitation_count("org_1") is None


@pytest.mark.asyncio
async def test_pending_invitation_count_none_and_warns_when_page_is_full(monkeypatch, caplog):
    """A full page means the true count may be truncated. A truncated read is
    an uncertain read — confidently returning a short count would let
    sync_team_seat_quantity write an undershooting absolute quantity to
    Stripe (a silent downgrade). Must return None (treated as unreadable),
    with a warning so the truncation is visible in the logs rather than a
    wrong number."""
    monkeypatch.setattr("core.config.settings.CLERK_SECRET_KEY", "sk_test_xyz")
    from core.services import clerk_admin

    data = [{"id": f"orginv_{i}"} for i in range(clerk_admin._PENDING_INVITATION_PAGE_LIMIT)]
    response = _make_response(200, json_body={"data": data, "total_count": len(data)})
    with patch(
        "core.services.clerk_admin.httpx.AsyncClient",
        new=lambda *a, **k: _fake_client(response),
    ):
        with caplog.at_level("WARNING"):
            result = await clerk_admin._pending_invitation_count("org_1")

    assert result is None
    assert any("truncated" in record.message for record in caplog.records)
