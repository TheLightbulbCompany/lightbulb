"""clerk_admin.create_organization_membership — the join link's Clerk call.

The behaviour worth pinning is the already-a-member branch: Clerk answers a
duplicate membership with a 4xx, and the join flow has to read that as success
or the owner's own link errors for them.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from core.services import clerk_admin


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=payload if payload is not None else {},
        request=httpx.Request("POST", "https://api.clerk.com/v1/organizations/org_1/memberships"),
    )


def _patched_post(response):
    """Patch the AsyncClient's post, with a live CLERK_SECRET_KEY."""
    client = AsyncMock()
    client.post = (
        AsyncMock(return_value=response) if not isinstance(response, Exception) else AsyncMock(side_effect=response)
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(httpx, "AsyncClient", return_value=ctx), client


@pytest.fixture(autouse=True)
def clerk_key():
    with patch.object(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test_xxx"):
        yield


@pytest.mark.asyncio
async def test_success_returns_joined_and_posts_the_member_role():
    patcher, client = _patched_post(_response(200, {"id": "orgmem_1"}))
    with patcher:
        result = await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1", role="org:member")
    assert result == {"status": "joined", "membership_id": "orgmem_1"}
    assert client.post.await_args.kwargs["json"] == {"user_id": "user_1", "role": "org:member"}


@pytest.mark.parametrize(
    "code",
    ["already_a_member_in_organization", "organization_membership_exists", "duplicate_record"],
)
@pytest.mark.asyncio
async def test_already_a_member_is_success_for_every_clerk_code(code):
    patcher, _ = _patched_post(_response(422, {"errors": [{"code": code}]}))
    with patcher:
        result = await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1")
    assert result["status"] == "already_member"


@pytest.mark.asyncio
async def test_member_cap_is_a_terminal_409_not_a_retryable_502():
    patcher, _ = _patched_post(
        _response(403, {"errors": [{"code": "organization_membership_quota_exceeded", "message": "quota"}]})
    )
    with patcher, pytest.raises(HTTPException) as err:
        await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1")
    assert err.value.status_code == 409
    assert err.value.detail == clerk_admin.MEMBER_CAP_REACHED


@pytest.mark.asyncio
async def test_other_clerk_failures_raise_without_echoing_clerk_internals():
    """The caller here is an arbitrary signed-in user, not an admin."""
    patcher, _ = _patched_post(_response(400, {"errors": [{"code": "form_param_nil", "message": "x"}]}))
    with patcher:
        with pytest.raises(HTTPException) as err:
            await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1")
    assert err.value.status_code == 502
    assert "form_param_nil" not in str(err.value.detail)


@pytest.mark.asyncio
async def test_transport_error_becomes_a_503():
    patcher, _ = _patched_post(httpx.ConnectError("boom"))
    with patcher:
        with pytest.raises(HTTPException) as err:
            await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1")
    assert err.value.status_code == 503


@pytest.mark.asyncio
async def test_stubs_without_a_clerk_key():
    with patch.object(clerk_admin.settings, "CLERK_SECRET_KEY", ""):
        result = await clerk_admin.create_organization_membership(org_id="org_1", user_id="user_1")
    assert result["status"] == "joined"
    assert result["stubbed"] is True
