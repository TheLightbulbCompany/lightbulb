"""Company deploy dispatch endpoint (buyer-open, fire-and-forget).

The deploy is FIRE-AND-FORGET: the route schedules ``run_company_deploy`` off
the request path and returns 202 with ``{started, agent_count}`` — there is no
job row and no status/rollback endpoint. Progress + the roster live in Teams.
The route is open to any workspace admin buyer and concurrency is the
owner-wide deploy claim the route takes before dispatching.

Follows the company-listing client/auth idiom from
``test_marketplace_company_listings.py`` — the conftest ``client`` fixture and
``admin_client`` below both use active workspace-admin contexts.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def admin_client(app):
    """TestClient authenticated as a workspace admin."""

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
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def deploy_claim():
    """Stub the owner-wide deploy claim + the pod-running read the company
    branch now makes — unit tests must not touch DynamoDB. Individual tests
    re-patch to exercise the refusal paths."""
    with (
        patch(
            "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
            new_callable=AsyncMock,
            return_value=("tok", None),
        ) as claim,
        patch(
            "routers.marketplace_listings.container_repo.release_owner_deploy_claim",
            new_callable=AsyncMock,
        ) as release,
        patch(
            "routers.marketplace_listings.container_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"owner_id": "org_admin", "status": "running"},
        ) as get_row,
    ):
        yield claim, release, get_row


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
    # no job_id (fire-and-forget). The task owns the claim from here.
    mock_run.assert_called_once_with(owner_id="org_admin", listing=listing, tar_bytes=b"COMPANYTAR", claim_token="tok")
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_company_deploy_skips_entitlement_size_reconcile(
    mock_get_published, mock_boto, mock_run, mock_create_task, admin_client
):
    """Company agent count never triggers a commercial size gate in deploy."""
    ecs = MagicMock()
    ecs.current_container_size = AsyncMock(return_value=("512", "2048"))
    ecs.resize_user_container = AsyncMock()
    mock_get_published.return_value = _company_listing(agent_count=15, min_plan="ultra_pro")
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"started": True, "agent_count": 15}
    mock_create_task.assert_called_once_with(mock_run.return_value)
    ecs.current_container_size.assert_not_called()
    ecs.resize_user_container.assert_not_called()


@patch("routers.marketplace_listings.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_priced_company_without_purchase_403(
    mock_get_published, mock_boto, mock_run, mock_create_task, mock_purchase, admin_client
):
    """Buyer launch closed the old bypass: the company branch used to return
    before the agent path's paid-entitlement check ever ran (harmless while
    admin-only). A priced company with NO purchase row is now 403'd before any
    claim/S3/dispatch work."""
    mock_get_published.return_value = _company_listing(price_cents=2000)
    mock_purchase.return_value = False

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 403, resp.text
    mock_purchase.assert_awaited_once_with(buyer_id="org_admin", listing_id="l1")
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_priced_company_with_purchase_202(
    mock_get_published, mock_boto, mock_run, mock_create_task, mock_purchase, admin_client
):
    """An entitled buyer (unrevoked purchase row) deploys a priced company 202."""
    mock_get_published.return_value = _company_listing(price_cents=2000)
    mock_purchase.return_value = True
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"started": True, "agent_count": 5}
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.marketplace_repo.has_unrevoked_purchase", new_callable=AsyncMock)
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_seller_deploys_own_priced_company_without_purchase(
    mock_get_published, mock_boto, mock_run, mock_create_task, mock_purchase, admin_client
):
    """Sellers don't buy their own listings — the identity bypass from the
    agent path applies to companies too."""
    listing = _company_listing(price_cents=2000)
    listing["seller_id"] = "user_admin"
    mock_get_published.return_value = listing
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = admin_client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    mock_purchase.assert_not_awaited()
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy", new_callable=MagicMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_non_admin_deploy_company_202(mock_get_published, mock_boto, mock_run, mock_create_task, client):
    """Buyer launch: an ordinary signed-in user (personal, email=None) deploys a
    standard company — no platform-admin gate."""
    mock_get_published.return_value = _company_listing()
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 202, resp.text
    mock_create_task.assert_called_once_with(mock_run.return_value)


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_company_deploy_refused_while_claim_held(
    mock_get_published, mock_boto, mock_run, mock_create_task, client, deploy_claim
):
    """A held owner-wide deploy claim (a hire or another company deploy in
    flight) refuses the dispatch 409 — no S3 read, no background task. A double
    POST minting the whole org twice is exactly what this fences."""
    mock_get_published.return_value = _company_listing()
    claim, _release, _get_row = deploy_claim
    claim.return_value = (None, "l_other")

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    assert "already running" in resp.json()["detail"]
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@pytest.mark.parametrize("row", [None, {"owner_id": "user_x", "status": "stopped"}])
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_company_deploy_refused_when_pod_not_running(
    mock_get_published, mock_boto, mock_run, mock_create_task, client, deploy_claim, row
):
    """No container row (fresh account that never opened the app) or a
    scaled-to-zero pod refuses 503 UP FRONT — the old shape was a 202 +
    confirmation card while the background job silently created nothing. No
    claim taken, no S3 read, no dispatch."""
    mock_get_published.return_value = _company_listing()
    claim, _release, get_row = deploy_claim
    get_row.return_value = row

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 503, resp.text
    assert "open the app" in resp.json()["detail"]
    claim.assert_not_awaited()
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_second_company_deploy_refused_409(
    mock_get_published, mock_boto, mock_run, mock_create_task, client, deploy_claim
):
    """ONE company package per workspace: an owner whose row carries the
    deployed_company marker is refused — same listing or a different one —
    before any claim/S3/dispatch work."""
    mock_get_published.return_value = _company_listing()
    claim, _release, get_row = deploy_claim
    get_row.return_value = {
        "owner_id": "user_x",
        "status": "running",
        "deployed_company": {"listing_id": "l_other", "name": "Acme Co", "deployed_at": 1},
    }

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    assert "already deployed Acme Co" in resp.json()["detail"]
    claim.assert_not_awaited()
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_one_company_fence_runs_before_artifact_work(
    mock_get_published, mock_boto, mock_run, mock_create_task, client, deploy_claim
):
    """An existing company is refused before artifact reads or dispatch."""
    mock_get_published.return_value = _company_listing(agent_count=15, min_plan="ultra_pro")
    _claim, _release, get_row = deploy_claim
    get_row.return_value = {
        "owner_id": "user_x",
        "status": "running",
        "deployed_company": {"listing_id": "l_first", "name": "First Co", "deployed_at": 1},
    }

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")

    assert resp.status_code == 409, resp.text
    assert "already deployed First Co" in resp.json()["detail"]
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()
