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
