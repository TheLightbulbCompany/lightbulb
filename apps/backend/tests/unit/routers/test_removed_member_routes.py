"""The member, invite and join-link doors are gone: a person is the tenant, so there is nobody to invite."""

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/orgs/org_x/join-link"),
        ("POST", "/api/v1/orgs/org_x/join-link"),
        ("DELETE", "/api/v1/orgs/org_x/join-link"),
        ("POST", "/api/v1/orgs/org_x/join"),
        ("GET", "/api/v1/orgs/org_x/invitations"),
        ("POST", "/api/v1/orgs/org_x/invitations"),
        ("DELETE", "/api/v1/orgs/org_x/invitations/inv_1"),
    ],
)
async def test_removed_routes_404(async_client, method, path):
    assert (await async_client.request(method, path)).status_code == 404
