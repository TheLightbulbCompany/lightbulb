"""Marketplace deploys are independent of commercial agent-count limits."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def admin_client(app):
    async def _mock():
        return AuthContext(
            user_id="user_admin",
            email="admin@thelightbulb.company",
            org_id="org_admin",
            org_role="org:admin",
        )

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _mock
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def deploy_claim():
    with (
        patch(
            "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
            new_callable=AsyncMock,
            return_value=("tok", None),
        ),
        patch(
            "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
            new_callable=AsyncMock,
        ),
        patch(
            "routers.marketplace_listings.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"owner_id": "org_admin", "status": "running"},
        ),
    ):
        yield


def _company_listing() -> dict:
    return {
        "listing_id": "l_large",
        "version": 1,
        "slug": "bigco",
        "name": "BigCo",
        "format": "company",
        "status": "published",
        "price_cents": 0,
        "tags": [],
        "seller_id": "__isol8__",
        "s3_prefix": "listings/l_large/v1/",
        "manifest_json": {
            "company": {
                "agent_count": 15,
                # Old manifests may retain this metadata; it is no longer an
                # admission decision at deploy time.
                "min_plan": "ultra_pro",
                "agent_slugs": ["a", "b", "c"],
            }
        },
    }


def _single_agent_listing() -> dict:
    return {
        "listing_id": "l_solo",
        "version": 1,
        "slug": "solo",
        "name": "Solo",
        "format": "agent",
        "status": "published",
        "price_cents": 0,
        "tags": [],
        "seller_id": "seller_x",
        "s3_prefix": "listings/l_solo/v1/",
        "manifest_json": {},
    }


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_large_company_deploy_does_not_consult_live_agent_count(
    mock_get_published,
    mock_boto,
    _mock_run,
    mock_create_task,
    admin_client,
    monkeypatch,
):
    mock_get_published.return_value = _company_listing()
    body = MagicMock()
    body.read.return_value = b"COMPANYTAR"
    mock_boto.return_value.get_object.return_value = {"Body": body}

    response = admin_client.post("/api/v1/marketplace/listings/bigco/deploy")

    assert response.status_code == 202, response.text
    assert response.json() == {"started": True, "agent_count": 15}
    mock_create_task.assert_called_once()
