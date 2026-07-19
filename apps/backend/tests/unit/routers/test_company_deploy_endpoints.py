"""Company deploy dispatch endpoint (admin-flagged, standard-only, fire-and-forget).

Replaces the "Company deploys are coming soon." 400 stub in ``deploy_listing``
with the real (platform-admin-gated) dispatch. The deploy is FIRE-AND-FORGET:
the route schedules ``run_company_deploy`` off the request path and returns 202
with ``{started, agent_count}`` — there is no job row and no status/rollback
endpoint. Progress + the roster live in Teams.

Follows the company-listing client/auth idiom from
``test_marketplace_company_listings.py`` — the conftest ``client`` fixture is a
personal (non-admin, email=None) caller; ``admin_client`` below is an
``@isol8.co`` platform admin.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def admin_client(app):
    """TestClient authenticated as an @isol8.co platform admin (personal ctx)."""

    async def _mock():
        return AuthContext(user_id="user_admin", email="admin@isol8.co")

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _mock
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _company_listing(
    *,
    listing_id: str = "l1",
    agent_count: int = 5,
    min_plan: str = "standard",
    agent_slugs: list[str] | None = None,
    price_cents: int = 0,
) -> dict:
    return {
        "listing_id": listing_id,
        "version": 1,
        "slug": "gstack",
        "name": "GStack",
        "format": "company",
        "status": "published",
        "price_cents": price_cents,
        "tags": [],
        "seller_id": "__isol8__",
        "s3_prefix": f"listings/{listing_id}/v1/",
        "manifest_json": {
            "company": {
                "agent_count": agent_count,
                "min_plan": min_plan,
                "agent_slugs": agent_slugs if agent_slugs is not None else ["ceo", "cto", "eng", "ops", "sales"],
            }
        },
    }


def _tar_body(data: bytes = b"COMPANYTAR"):
    body = MagicMock()
    body.read.return_value = data
    return {"Body": body}


# ---------------------------------------------------------------------------
# POST /listings/{slug}/deploy — company branch (fire-and-forget)
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_standard_company_202(mock_get_published, mock_boto, mock_run, mock_create_task, admin_client):
    """Admin deploying a published standard company gets 202 + {started, agent_count},
    and the background deploy is scheduled with the right coroutine args."""
    listing = _company_listing()
    mock_get_published.return_value = listing
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"started": True, "agent_count": 5}

    # Background task fired with the run_company_deploy coroutine, right args —
    # no job_id (fire-and-forget).
    mock_run.assert_called_once_with(owner_id="user_admin", listing=listing, tar_bytes=b"COMPANYTAR")
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_priced_company_short_circuits_entitlement(
    mock_get_published, mock_boto, mock_run, mock_create_task, mock_purchases, admin_client
):
    """The company branch returns BEFORE the agent-path paid-entitlement check.

    A priced (price_cents=2000) company listing with NO purchase row still
    deploys 202 for an admin — proving the company branch short-circuits ahead
    of the entitlement gate (which would otherwise 403). The purchases table is
    never queried.
    """
    mock_get_published.return_value = _company_listing(price_cents=2000)
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")
    # Real dict (not a bare MagicMock): has_unrevoked_purchase pages on
    # resp.get("LastEvaluatedKey"), and a MagicMock's truthy return there
    # spins the pagination loop forever.
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"started": True, "agent_count": 5}
    # Entitlement path never touched — company branch returned first.
    mock_purchases.return_value.query.assert_not_called()
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_non_admin_deploy_company_403(mock_get_published, mock_boto, mock_run, mock_create_task, client):
    """A non-admin (personal, email=None) is 403'd server-side — no background
    task, no S3 read."""
    mock_get_published.return_value = _company_listing()

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 403, resp.text
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_ultra_pro_company_409(mock_get_published, mock_boto, mock_run, mock_create_task, admin_client):
    """A >10-agent (ultra_pro) company is rejected 409 'Requires Ultra Pro' —
    the plan gate lives inside the admin-gated branch, before any S3/dispatch work."""
    mock_get_published.return_value = _company_listing(agent_count=15, min_plan="ultra_pro")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    assert "ultra pro" in resp.json()["detail"].lower()
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_inconsistent_agent_count_409(
    mock_get_published, mock_boto, mock_run, mock_create_task, admin_client
):
    """Belt-and-suspenders: min_plan='standard' but agent_count>10 still 409s."""
    mock_get_published.return_value = _company_listing(agent_count=11, min_plan="standard")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()
