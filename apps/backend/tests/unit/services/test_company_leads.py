"""company_leads.search_people — the first tier-2 backend read.

Pins the three contracts the Leads tab depends on: no key is a quiet
`connected: False` (never an exception), Apollo's masked-surname / has_email
shape is projected without inventing fields it does not return, and a non-200
raises rather than rendering as "nobody".
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_leads  # noqa: E402


@pytest.mark.asyncio
async def test_no_key_is_connected_false_not_an_error():
    with patch.object(company_leads.integration_secret_store, "get", AsyncMock(return_value=None)):
        out = await company_leads.search_people("owner", query="heads of growth")
    assert out == {"connected": False, "data": [], "total": 0, "page": 1, "has_more": False}


def _resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("POST", company_leads._URL))


@pytest.mark.asyncio
async def test_projects_apollo_shape_honestly():
    """Apollo's free search returns a MASKED surname and a has_email flag —
    not an email, not a LinkedIn URL. Nothing is invented."""
    apollo = {
        # Apollo's envelope: the count is nested under `pagination`, never at
        # the root. A root-level read silently reports 0 and never pages.
        "pagination": {"page": 1, "per_page": 25, "total_entries": 40, "total_pages": 2},
        "people": [
            {
                "id": "p1",
                "first_name": "Ada",
                "last_name_obfuscated": "L.",
                "title": "Head of Growth",
                "organization": {"name": "Lovelace Ltd"},
                "has_email": True,
                "email": "should-not-be-trusted-if-present@example.com",
                "city": "London",
                "country": "United Kingdom",
            }
        ],
    }
    with (
        patch.object(company_leads.integration_secret_store, "get", AsyncMock(return_value={"token": "k"})),
        patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_resp(200, apollo))) as post,
    ):
        out = await company_leads.search_people("owner", query="heads of growth", page=1)

    # Query string, not body — verified against Apollo's reference.
    kwargs = post.call_args.kwargs
    assert kwargs["params"]["q_keywords"] == "heads of growth"
    assert kwargs["headers"]["X-Api-Key"] == "k"
    assert "json" not in kwargs

    assert out["connected"] is True and out["total"] == 40 and out["has_more"] is True
    row = out["data"][0]
    assert row == {
        "id": "p1",
        "first_name": "Ada",
        "last_name": "L.",
        "title": "Head of Growth",
        "organization": "Lovelace Ltd",
        "has_email": True,
        "city": "London",
        "country": "United Kingdom",
    }
    assert "email" not in row


@pytest.mark.asyncio
async def test_non_200_raises_rather_than_rendering_nobody():
    with (
        patch.object(company_leads.integration_secret_store, "get", AsyncMock(return_value={"token": "dead"})),
        patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_resp(401, {"error": "bad key"}))),
    ):
        with pytest.raises(company_leads.ApolloError) as e:
            await company_leads.search_people("owner", query="anyone")
    assert e.value.status == 401


@pytest.mark.asyncio
async def test_per_page_is_capped():
    with (
        patch.object(company_leads.integration_secret_store, "get", AsyncMock(return_value={"token": "k"})),
        patch.object(
            httpx.AsyncClient,
            "post",
            AsyncMock(return_value=_resp(200, {"people": [], "pagination": {"total_entries": 0}})),
        ) as post,
    ):
        await company_leads.search_people("owner", query="x", per_page=999)
    assert post.call_args.kwargs["params"]["per_page"] == company_leads.MAX_PER_PAGE


@pytest.mark.asyncio
async def test_has_more_stops_at_the_last_page_the_router_serves():
    """A broad query can have thousands of matches; page 50 is the last one
    Apollo (and our router) will serve, so it must not advertise a page 51."""
    apollo = {"pagination": {"total_entries": 5000}, "people": []}
    with (
        patch.object(company_leads.integration_secret_store, "get", AsyncMock(return_value={"token": "k"})),
        patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_resp(200, apollo))),
    ):
        last = await company_leads.search_people("owner", query="founders", page=company_leads.MAX_PAGE)
        before = await company_leads.search_people("owner", query="founders", page=company_leads.MAX_PAGE - 1)
    assert last["has_more"] is False
    assert before["has_more"] is True
