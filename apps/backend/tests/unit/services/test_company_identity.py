"""company_identity: initial name generation + atomic slug allocation."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_identity  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Robotics", "acme-robotics"),
        ("  Acme   Robotics!!  ", "acme-robotics"),
        ("Ünicode Ço", "unicode-co"),
        ("---", ""),
        ("A" * 60, "a" * 32),
    ],
)
def test_slugify(raw, expected):
    assert company_identity.slugify(raw) == expected


@pytest.mark.parametrize(
    "email,expected",
    [
        ("jane.doe@gmail.com", "Jane Doe"),
        ("jane.doe+tag@gmail.com", "Jane Doe"),
        ("jane_doe-smith@x.io", "Jane Doe Smith"),
        ("j@x.io", "J"),
    ],
)
def test_prettify_email_local_part(email, expected):
    assert company_identity.prettify_email_local_part(email) == expected


@pytest.mark.asyncio
async def test_generate_name_uses_clerk_org_name_for_orgs(monkeypatch):
    async def fake_get_org(org_id):
        return {"id": org_id, "name": "Acme Robotics", "slug": "acme-robotics"}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(org_id="org_123", owner_email="owner@example.com")
    assert name == "Acme Robotics"


@pytest.mark.asyncio
async def test_generate_name_falls_back_to_email_when_clerk_returns_nothing(monkeypatch):
    async def fake_get_org(org_id):
        return None

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(org_id="org_123", owner_email="jane.doe@example.com")
    assert name == "Jane Doe"


@pytest.mark.asyncio
async def test_generate_name_for_personal_owner_uses_email(monkeypatch):
    """A personal owner has no org, so Clerk is never consulted."""
    called = []

    async def fake_get_org(org_id):
        called.append(org_id)
        return {"name": "should not be used"}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(org_id=None, owner_email="jane.doe@example.com")
    assert name == "Jane Doe"
    assert called == []


@pytest.mark.asyncio
async def test_allocate_slug_claims_the_plain_slug_when_free(monkeypatch):
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="Acme Robotics")
    assert slug == "acme-robotics"
    assert attempts == ["acme-robotics"]


@pytest.mark.asyncio
async def test_allocate_slug_suffixes_on_collision(monkeypatch):
    async def fake_reserve(*, slug, owner_id):
        return slug == "acme-3"

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="Acme")
    assert slug == "acme-3"


@pytest.mark.asyncio
async def test_allocate_slug_avoids_reserved_words(monkeypatch):
    """Reserved local parts (support@, admin@…) must never be handed out."""
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="Support")
    assert slug != "support"
    assert attempts[0] != "support"


@pytest.mark.asyncio
async def test_allocate_slug_falls_back_when_name_slugifies_to_empty(monkeypatch):
    async def fake_reserve(*, slug, owner_id):
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="---")
    assert slug
    assert company_identity.slugify(slug) == slug


@pytest.mark.asyncio
async def test_allocate_slug_returns_none_when_repo_fails(monkeypatch):
    """Slug allocation is best-effort — provisioning must survive it."""

    async def boom(**kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", boom)

    assert await company_identity.allocate_company_slug(owner_id="user_1", name="Acme") is None
