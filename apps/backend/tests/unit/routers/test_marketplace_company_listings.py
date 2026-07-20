"""format="company" listings: searchable, not deployable until the company deploy engine ships."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def unauth_client():
    """Plain TestClient without auth override (search allows anonymous callers).

    Defensively clears app.dependency_overrides on enter + exit so a prior
    suite that left an auth override behind cannot leak into these tests.
    Mirrors the fixture in test_marketplace_listings.py.
    """
    from main import app

    app.dependency_overrides.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _company_listing(*, price_cents: int = 0, listing_id: str = "l1") -> dict:
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
        "manifest_json": {"company": {"agent_count": 5, "min_plan": "standard"}},
    }


@patch("routers.marketplace_listings.marketplace_search.search", new_callable=AsyncMock)
def test_search_accepts_company_format(mock_search, unauth_client):
    """?format=company must pass the query-param pattern (previously 422)
    and thread through to the search service so company rows come back."""
    mock_search.return_value = [_company_listing()]
    resp = unauth_client.get("/api/v1/marketplace/listings/search?format=company")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items and items[0]["format"] == "company"
    mock_search.assert_called_once()
    assert mock_search.call_args.kwargs["format"] == "company"


@patch("core.repositories.marketplace_repo._purchases_table")
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_company_listing_returns_400(mock_get_published, mock_get_catalog, mock_boto, mock_purchases, client):
    """Deploying a published company listing 400s until the company deploy
    engine ships.

    The row is priced so the test also pins guard ordering: without the
    guard, a paid listing with no purchase row would 403 at the entitlement
    check — a 400 with the purchases table never queried proves the guard
    runs after the listing resolves but before any entitlement work.
    """
    mock_get_published.return_value = _company_listing(price_cents=2000)
    # Real dict (not a bare MagicMock): has_unrevoked_purchase pages on
    # resp.get("LastEvaluatedKey"), and a MagicMock's truthy return there
    # spins the pagination loop forever.
    mock_purchases.return_value.query.return_value = {"Items": []}

    resp = client.post("/api/v1/marketplace/listings/gstack/deploy")
    assert resp.status_code == 400, resp.text
    assert "coming soon" in resp.json()["detail"].lower()
    # Guard fired before entitlement / S3 / catalog work.
    mock_purchases.return_value.query.assert_not_called()
    mock_boto.assert_not_called()
    mock_get_catalog.assert_not_called()
