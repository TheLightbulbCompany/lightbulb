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


# The "Company deploys are coming soon." 400-stub test was removed when the stub
# was replaced by the real admin-gated dispatch — that behavior is superseded by
# the company-deploy endpoint tests in test_company_deploy_endpoints.py.
