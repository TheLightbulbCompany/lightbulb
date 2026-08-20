"""Company-deploy plan gate: an Ultra company deploys for an ultra_pro owner,
409s for a standard owner.

The company-deploy branch of ``deploy_listing`` used to hard-409 every company
needing Ultra (``min_plan != "standard"`` or ``agent_count`` over the standard
cap). PR3 flips it to a plan gate: the 409 fires ONLY when the listing needs
Ultra AND the owner is not on ``ultra_pro``. The old ``require_platform_admin``
launch gate has since been removed — the route is buyer-open — but these tests
still authenticate as a platform admin and vary only the billing plan (admin
no longer changes the route's behaviour).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402


@pytest.fixture
def admin_client(app):
    """TestClient authenticated as an @isol8.co platform admin (personal ctx),
    resolve_owner_id -> user_admin."""

    async def _mock():
        return AuthContext(user_id="user_admin", email="admin@isol8.co")

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _mock
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def deploy_claim():
    """Stub the owner-wide deploy claim (the company branch takes it before
    dispatch) — unit tests must not touch DynamoDB. The single-agent tests
    below re-patch these per-test where the claim is the subject."""
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
            return_value={"owner_id": "user_admin", "status": "running"},
        ),
    ):
        yield


def _ultra_company_listing() -> dict:
    """A published company listing that needs Ultra (>10 agents, min_plan=ultra_pro)."""
    return {
        "listing_id": "l_ultra",
        "version": 1,
        "slug": "bigco",
        "name": "BigCo",
        "format": "company",
        "status": "published",
        "price_cents": 0,
        "tags": [],
        "seller_id": "__isol8__",
        "s3_prefix": "listings/l_ultra/v1/",
        "manifest_json": {
            "company": {
                "agent_count": 15,
                "min_plan": "ultra_pro",
                "agent_slugs": ["a", "b", "c"],
            }
        },
    }


def _tar_body(data: bytes = b"COMPANYTAR"):
    body = MagicMock()
    body.read.return_value = data
    return {"Body": body}


def _ecs_mock(*, current_size):
    """ecs manager stub: the running box's ACTUAL size + resize.

    The router derives the desired Ultra size inline from the ULTRA_* constants
    (the plan gate already confirmed ultra_pro), so it does NOT call
    _size_for_owner here — only current_container_size + resize_user_container."""
    ecs = MagicMock()
    ecs.current_container_size = AsyncMock(return_value=current_size)
    ecs.resize_user_container = AsyncMock()
    return ecs


@patch("routers.marketplace_listings.get_ecs_manager")
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.billing_repo.get_by_owner_id", new_callable=AsyncMock)
def test_ultra_pro_owner_deploys_ultra_company_202(
    mock_billing, mock_get_published, mock_boto, mock_run, mock_create_task, mock_get_ecs, admin_client
):
    """ultra_pro owner + Ultra listing + Ultra-sized box -> passes the plan gate,
    size reconcile is a no-op (box already 2 vCPU / 8 GB), and the deploy is
    fire-and-forget dispatched (202). No resize."""
    mock_billing.return_value = {"owner_id": "user_admin", "plan": "ultra_pro"}
    mock_get_published.return_value = _ultra_company_listing()
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")
    ecs = _ecs_mock(current_size=("2048", "8192"))
    mock_get_ecs.return_value = ecs

    resp = admin_client.post("/api/v1/marketplace/listings/bigco/deploy")

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"started": True, "agent_count": 15}
    mock_create_task.assert_called_once()
    ecs.resize_user_container.assert_not_called()


@patch("routers.marketplace_listings.get_ecs_manager")
@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.billing_repo.get_by_owner_id", new_callable=AsyncMock)
def test_ultra_pro_owner_undersized_box_resizes_and_409(
    mock_billing, mock_get_published, mock_boto, mock_run, mock_create_task, mock_get_ecs, admin_client
):
    """ultra_pro owner + Ultra listing but the running box is STILL standard-sized
    (a swallowed upgrade-webhook resize) -> the deploy is refused: the size
    reconcile kicks resize_user_container to Ultra and returns 409 'resizing,
    retry', dispatching nothing and never touching S3."""
    mock_billing.return_value = {"owner_id": "user_admin", "plan": "ultra_pro"}
    mock_get_published.return_value = _ultra_company_listing()
    ecs = _ecs_mock(current_size=("512", "2048"))
    mock_get_ecs.return_value = ecs

    resp = admin_client.post("/api/v1/marketplace/listings/bigco/deploy")

    assert resp.status_code == 409, resp.text
    assert "resizing" in resp.json()["detail"].lower()
    ecs.resize_user_container.assert_called_once_with("user_admin", new_cpu="2048", new_memory="8192")
    # Deploy refused: no S3 read, no background dispatch.
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.billing_repo.get_by_owner_id", new_callable=AsyncMock)
def test_standard_owner_ultra_company_409(
    mock_billing, mock_get_published, mock_boto, mock_run, mock_create_task, admin_client
):
    """standard owner + Ultra listing -> still 409 'Requires Ultra Pro', before
    any S3/dispatch work."""
    mock_billing.return_value = {"owner_id": "user_admin", "plan": "standard"}
    mock_get_published.return_value = _ultra_company_listing()

    resp = admin_client.post("/api/v1/marketplace/listings/bigco/deploy")

    assert resp.status_code == 409, resp.text
    assert "ultra pro" in resp.json()["detail"].lower()
    mock_boto.assert_not_called()
    mock_run.assert_not_called()
    mock_create_task.assert_not_called()


# ---------------------------------------------------------------------------
# Agent budget on the deploy paths
# ---------------------------------------------------------------------------


def _standard_company_listing(agent_count: int = 5) -> dict:
    """A company listing that needs Ultra on NOBODY's reading of its own size."""
    listing = _ultra_company_listing()
    listing["listing_id"] = "l_small"
    listing["slug"] = "smallco"
    listing["name"] = "SmallCo"
    listing["manifest_json"] = {"company": {"agent_count": agent_count, "min_plan": "standard", "agent_slugs": ["a"]}}
    return listing


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.count_paperclip_agents_for_owner", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.billing_repo.get_by_owner_id", new_callable=AsyncMock)
def test_company_deploy_budgets_the_sum_not_just_the_package(
    mock_billing, mock_get_published, mock_count, mock_create_task, admin_client
):
    """company_deploy_service ADDS its agents to the existing team rather than
    replacing it, so a 'standard' package can still put a standard owner over
    the cap. 8 existing + a 5-agent package = 13 -> needs Ultra -> 409.

    Without the sum this returns 202 and lands 13 agents on the small box.
    """
    mock_billing.return_value = {"owner_id": "user_admin", "plan": "standard"}
    mock_get_published.return_value = _standard_company_listing(agent_count=5)
    mock_count.return_value = 8

    resp = admin_client.post("/api/v1/marketplace/listings/smallco/deploy")

    assert resp.status_code == 409, resp.text
    assert "Ultra Pro" in resp.json()["detail"]
    mock_create_task.assert_not_called()


@patch("routers.marketplace_listings.asyncio.create_task")
@patch("routers.marketplace_listings.company_deploy_service.run_company_deploy")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.count_paperclip_agents_for_owner", new_callable=AsyncMock)
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
@patch("routers.marketplace_listings.billing_repo.get_by_owner_id", new_callable=AsyncMock)
def test_company_deploy_undeterminable_count_does_not_block(
    mock_billing, mock_get_published, mock_count, mock_boto, mock_run, mock_create_task, admin_client
):
    """0 from the count means Paperclip was unreachable, NOT an empty roster.

    It must drop out of the sum rather than read as 'no existing agents' — and
    equally must not be treated as over-budget. Fail open, like every other
    budget site: the deploy proceeds.
    """
    mock_billing.return_value = {"owner_id": "user_admin", "plan": "standard"}
    mock_get_published.return_value = _standard_company_listing(agent_count=5)
    mock_count.return_value = 0
    mock_boto.return_value.get_object.return_value = _tar_body(b"COMPANYTAR")

    resp = admin_client.post("/api/v1/marketplace/listings/smallco/deploy")

    assert resp.status_code == 202, resp.text
    mock_create_task.assert_called_once()


def _single_agent_listing() -> dict:
    """A free, published, single-agent (non-company) listing."""
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


@patch("routers.marketplace_listings.container_repo.release_owner_deploy_claim", new_callable=AsyncMock)
@patch(
    "routers.marketplace_listings.container_repo.try_claim_owner_deploy",
    new_callable=AsyncMock,
    return_value=("claim", None),
)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings._fetch_agent_artifact")
@patch("routers.marketplace_listings._live_install_of", return_value=None)
@patch("routers.marketplace_listings.is_at_agent_budget", new_callable=AsyncMock, return_value=True)
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_single_agent_hire_at_cap_allows_only_an_existing_operation(
    mock_get_published,
    mock_at_budget,
    _mock_live,
    mock_fetch,
    mock_create,
    _mock_claim,
    mock_release,
    admin_client,
):
    """At the cap, a missing operation 409s without creating another agent."""
    from core.services.paperclip_admin_client import PaperclipAgentCreateReplayMiss

    mock_get_published.return_value = _single_agent_listing()
    mock_fetch.return_value = (b"tar", {"agent": {"name": "Solo"}})
    mock_create.side_effect = PaperclipAgentCreateReplayMiss(
        "replay miss",
        status_code=409,
        body='{"code":"agent_create_idempotency_replay_miss"}',
    )

    resp = admin_client.post("/api/v1/marketplace/listings/solo/deploy")

    assert resp.status_code == 409, resp.text
    mock_at_budget.assert_awaited_once()
    mock_create.assert_awaited_once()
    assert mock_create.await_args.kwargs["idempotency_replay_only"] is True
    mock_release.assert_awaited_once()


@patch("routers.marketplace_listings._live_install_of")
@patch("routers.marketplace_listings.is_at_agent_budget", new_callable=AsyncMock, return_value=True)
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_already_installed_retry_is_not_refused_at_the_limit(
    mock_get_published, mock_at_budget, mock_live, admin_client
):
    """ORDERING GUARANTEE — the P2 regression this locks in.

    A deploy whose response was lost leaves the owner installed but retrying.
    The budget gate must sit BELOW the already-installed short-circuit, or that
    retry is answered with "upgrade to Ultra Pro" for an agent they already have
    and that this call would not create. Moving the gate back above the
    short-circuit reintroduces the bug and fails here.
    """
    mock_get_published.return_value = _single_agent_listing()
    mock_live.return_value = {"agent_id": "agent-existing", "template_version": 1}

    resp = admin_client.post("/api/v1/marketplace/listings/solo/deploy")

    assert resp.status_code == 200, resp.text
    assert resp.json()["already_installed"] is True
    assert resp.json()["agent_id"] == "agent-existing"
    mock_at_budget.assert_not_awaited()
