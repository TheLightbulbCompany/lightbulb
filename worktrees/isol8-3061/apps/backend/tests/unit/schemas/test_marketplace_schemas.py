"""Tests for marketplace Pydantic schemas."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from schemas import marketplace  # noqa: E402


def test_listing_create_valid():
    payload = marketplace.ListingCreate(
        slug="my-cool-agent",
        name="My Cool Agent",
        description_md="Does cool things.",
        format="openclaw",
        price_cents=2000,
        tags=["sales", "outreach"],
    )
    assert payload.slug == "my-cool-agent"
    assert payload.format == "openclaw"


def test_listing_create_rejects_invalid_slug():
    with pytest.raises(ValidationError):
        marketplace.ListingCreate(
            slug="My Cool Agent",  # uppercase + space
            name="My Cool Agent",
            description_md="x",
            format="openclaw",
            price_cents=0,
            tags=[],
        )


def test_listing_create_rejects_too_many_tags():
    with pytest.raises(ValidationError):
        marketplace.ListingCreate(
            slug="x",
            name="My Skill",
            description_md="x",
            format="openclaw",
            price_cents=0,
            tags=["a", "b", "c", "d", "e", "f"],  # 6 tags, max is 5
        )


def test_listing_create_rejects_price_above_2000():
    """Per design doc P3 plaintext price ceiling is $20.00 v1."""
    with pytest.raises(ValidationError):
        marketplace.ListingCreate(
            slug="my-skill",
            name="My Skill",
            description_md="x",
            format="openclaw",
            price_cents=2001,
            tags=[],
        )


def test_listing_create_rejects_company_format():
    """Sellers cannot publish company listings — those are registry-ingested
    by the curated pipeline. A seller POSTing format="company" must 422."""
    with pytest.raises(ValidationError):
        marketplace.ListingCreate(
            slug="my-company",
            name="My Company",
            description_md="x",
            format="company",
            price_cents=0,
            tags=[],
        )


def test_preview_response_accepts_company_format():
    """The admin moderation preview response must accept company rows, or
    the inspect/takedown surface 500s on exactly the mirrored third-party
    content that most needs oversight."""
    resp = marketplace.ListingPreviewResponse(
        listing_id="lst_x",
        version=1,
        slug="gstack",
        name="GStack",
        seller_id="__isol8__",
        format="company",
        status="published",
        price_cents=0,
        tags=["company"],
        manifest={"company": {"agent_count": 5}},
        file_tree=[],
        safety_flags=[],
    )
    assert resp.format == "company"
