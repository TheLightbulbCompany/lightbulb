"""Company deploy dispatch + job-status/rollback endpoints (admin-flagged, standard-only).

Replaces the "Company deploys are coming soon." 400 stub in ``deploy_listing``
with the real (platform-admin-gated) dispatch, plus the two owner-scoped job
endpoints. Follows the company-listing client/auth idiom from
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
# POST /listings/{slug}/deploy — company branch
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.company_deploy_repo.create_job", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_standard_company_202(
    mock_get_published, mock_boto, mock_create_job, mock_run, mock_create_task, admin_client
):
    """Admin deploying a published standard company gets 202 + job_id, and the
    background deploy is scheduled with the right coroutine args."""
    listing = _company_listing()
    mock_get_published.return_value = listing
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")
    mock_create_job.return_value = "cdpl_abc123"

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"job_id": "cdpl_abc123"}

    mock_create_job.assert_awaited_once()
    kwargs = mock_create_job.await_args.kwargs
    assert kwargs["owner_id"] == "user_admin"
    assert kwargs["listing_id"] == "l1"
    assert kwargs["listing_version"] == 1
    assert kwargs["company_slug"] == "gstack"
    assert kwargs["agent_slugs"] == ["ceo", "cto", "eng", "ops", "sales"]
    assert isinstance(kwargs["now_iso"], str) and kwargs["now_iso"]

    # Background task fired with the run_company_deploy coroutine, right args.
    mock_run.assert_called_once_with(
        job_id="cdpl_abc123", owner_id="user_admin", listing=listing, tar_bytes=b"COMPANYTAR"
    )
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.company_deploy_repo.create_job", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_priced_company_short_circuits_entitlement(
    mock_get_published, mock_boto, mock_create_job, mock_run, mock_create_task, mock_purchases, admin_client
):
    """The company branch returns BEFORE the agent-path paid-entitlement check.

    A priced (price_cents=2000) company listing with NO purchase row still
    deploys 202 for an admin — proving the company branch short-circuits ahead
    of the entitlement gate (which would otherwise 403). The purchases table is
    never queried.
    """
    mock_get_published.return_value = _company_listing(price_cents=2000)
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")
    mock_create_job.return_value = "cdpl_paid"
    # Real dict (not a bare MagicMock): has_unrevoked_purchase pages on
    # resp.get("LastEvaluatedKey"), and a MagicMock's truthy return there
    # spins the pagination loop forever.
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"job_id": "cdpl_paid"}
    # Entitlement path never touched — company branch returned first.
    mock_purchases.return_value.query.assert_not_called()
    mock_create_job.assert_awaited_once()
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.company_deploy_repo.create_job", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_non_admin_deploy_company_403(
    mock_get_published, mock_boto, mock_create_job, mock_run, mock_create_task, client
):
    """A non-admin (personal, email=None) is 403'd server-side — no job created,
    no background task, no S3 read."""
    mock_get_published.return_value = _company_listing()

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 403, resp.text
    mock_boto.assert_not_called()
    mock_create_job.assert_not_awaited()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_repo.create_job", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_ultra_pro_company_409(
    mock_get_published, mock_boto, mock_create_job, mock_create_task, admin_client
):
    """A >10-agent (ultra_pro) company is rejected 409 'Requires Ultra Pro' —
    the plan gate lives inside the admin-gated branch, before any S3/job work."""
    mock_get_published.return_value = _company_listing(agent_count=15, min_plan="ultra_pro")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    assert "ultra pro" in resp.json()["detail"].lower()
    mock_boto.assert_not_called()
    mock_create_job.assert_not_awaited()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_repo.create_job", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_admin_deploy_inconsistent_agent_count_409(
    mock_get_published, mock_boto, mock_create_job, mock_create_task, admin_client
):
    """Belt-and-suspenders: min_plan='standard' but agent_count>10 still 409s."""
    mock_get_published.return_value = _company_listing(agent_count=11, min_plan="standard")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    mock_create_job.assert_not_awaited()
    mock_create_task.assert_not_called()


# ---------------------------------------------------------------------------
# GET /company-deploys/{job_id} — owner-scoped status
# ---------------------------------------------------------------------------


def _job_row(*, owner_id: str, job_id: str = "cdpl_x") -> dict:
    return {
        "job_id": job_id,
        "owner_id": owner_id,
        "listing_id": "l1",
        "listing_version": 1,
        "company_slug": "gstack",
        "status": "running",
        "agents": [{"slug": "ceo", "paperclip_id": None, "state": "pending"}],
        "routine_ids": [],
        "error": None,
        "created_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-19T00:00:00+00:00",
    }


@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_get_job_returns_row_for_owner(mock_get_job, client):
    """Owner (personal user_test_123) reads their own job row in full."""
    row = _job_row(owner_id="user_test_123")
    mock_get_job.return_value = row

    resp = client.get("/api/v1/marketplace/company-deploys/cdpl_x")

    assert resp.status_code == 200, resp.text
    assert resp.json() == row
    mock_get_job.assert_awaited_once_with("cdpl_x")


@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_get_job_missing_404(mock_get_job, client):
    mock_get_job.return_value = None
    resp = client.get("/api/v1/marketplace/company-deploys/nope")
    assert resp.status_code == 404, resp.text


@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_get_job_other_owner_403(mock_get_job, client):
    """A caller cannot read another owner's job."""
    mock_get_job.return_value = _job_row(owner_id="user_other")
    resp = client.get("/api/v1/marketplace/company-deploys/cdpl_x")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# POST /company-deploys/{job_id}/rollback — owner-scoped teardown
# ---------------------------------------------------------------------------


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.rollback_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_rollback_owner_202_schedules_task(mock_get_job, mock_rollback, mock_create_task, client):
    mock_get_job.return_value = _job_row(owner_id="user_test_123")

    resp = client.post("/api/v1/marketplace/company-deploys/cdpl_x/rollback")

    assert resp.status_code == 202, resp.text
    mock_rollback.assert_called_once_with(job_id="cdpl_x")
    mock_create_task.assert_called_once_with(mock_rollback.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.rollback_company_deploy")
@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_rollback_missing_404(mock_get_job, mock_rollback, mock_create_task, client):
    mock_get_job.return_value = None
    resp = client.post("/api/v1/marketplace/company-deploys/nope/rollback")
    assert resp.status_code == 404, resp.text
    mock_rollback.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.rollback_company_deploy")
@patch("routers.marketplace_listings.company_deploy_repo.get_job", new_callable=AsyncMock)
def test_rollback_other_owner_403(mock_get_job, mock_rollback, mock_create_task, client):
    mock_get_job.return_value = _job_row(owner_id="user_other")
    resp = client.post("/api/v1/marketplace/company-deploys/cdpl_x/rollback")
    assert resp.status_code == 403, resp.text
    mock_rollback.assert_not_called()
    mock_create_task.assert_not_called()
