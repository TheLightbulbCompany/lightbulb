"""company_identity: initial name generation + atomic slug allocation."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import company_identity  # noqa: E402


@pytest.fixture(autouse=True)
def _no_existing_slug_reservation(monkeypatch):
    """`allocate_company_slug` now checks `get_by_owner` before deriving a
    base slug (re-entry stability — see FIX 8). Default every test to "no
    existing reservation" so pre-existing allocate_slug tests, which only
    mock `reserve`, keep exercising the fresh-allocation path unchanged. The
    dedicated re-entry test overrides this."""

    async def fake_get_by_owner(owner_id):
        return None

    monkeypatch.setattr(company_identity.company_slug_repo, "get_by_owner", fake_get_by_owner)


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
async def test_allocate_slug_never_suffixes_a_taken_slug(monkeypatch):
    """Owner ruling 2026-09-24: no `acme-2`. A taken derived slug means no address."""
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return False

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    assert await company_identity.allocate_company_slug(owner_id="org_1", name="Acme") is None
    assert attempts == ["acme"]


@pytest.mark.asyncio
async def test_allocate_slug_never_hands_out_a_reserved_word(monkeypatch):
    """Reserved local parts (support@, admin@…) are never handed out, suffixed or not."""
    attempts = []

    async def fake_reserve(*, slug, owner_id):
        attempts.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    assert await company_identity.allocate_company_slug(owner_id="org_1", name="Support") is None
    assert attempts == []


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


@pytest.mark.asyncio
async def test_allocate_slug_returns_none_instead_of_raising_for_non_string_name(monkeypatch):
    """`base = slugify(name)` must run inside the try — never raise, even for bad input."""

    async def fake_reserve(*, slug, owner_id):
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    assert await company_identity.allocate_company_slug(owner_id="user_1", name=None) is None


@pytest.mark.asyncio
async def test_generate_name_falls_back_instead_of_raising_for_non_string_owner_email(monkeypatch):
    """`prettify_email_local_part(owner_email)` sat outside any guard — a
    non-string `owner_email` (personal-owner path, no org_id) must not raise,
    per the "Never raises" docstring contract."""
    name = await company_identity.generate_company_name(org_id=None, owner_email=None)
    assert name == "My Company"


@pytest.mark.asyncio
async def test_allocate_slug_returns_existing_reservation_without_reserving_again(monkeypatch):
    """`provision_org` re-entry after a partial failure must not mint a
    second, never-released slug row for the same owner."""
    reserve_calls = []

    async def fake_get_by_owner(owner_id):
        return {"slug": "acme-existing", "owner_id": owner_id}

    async def fake_reserve(*, slug, owner_id):
        reserve_calls.append(slug)
        return True

    monkeypatch.setattr(company_identity.company_slug_repo, "get_by_owner", fake_get_by_owner)
    monkeypatch.setattr(company_identity.company_slug_repo, "reserve", fake_reserve)

    slug = await company_identity.allocate_company_slug(owner_id="user_1", name="A Totally Different Name")
    assert slug == "acme-existing"
    assert reserve_calls == []


@pytest.mark.asyncio
async def test_generate_name_falls_back_to_default_when_email_local_part_is_empty(monkeypatch):
    """A pathological `owner_email` like `"@example.com"` prettifies to an
    empty string (`prettify_email_local_part` has nothing to work with) —
    `generate_company_name` must never return "". A downstream
    `create_company(name="")` call sits OUTSIDE the naming try/except and
    would 400, failing a net-new owner's provision."""
    name = await company_identity.generate_company_name(org_id=None, owner_email="@example.com")
    assert name
    assert name.strip()


@pytest.mark.asyncio
async def test_generate_name_falls_back_to_default_for_org_with_empty_name_and_pathological_email(monkeypatch):
    """Same emptiness bug, reached via the Clerk-org path: Clerk returns an
    org with a blank name, so the function falls through to the email
    fallback — which must still not surface an empty string."""

    async def fake_get_org(org_id):
        return {"id": org_id, "name": "  "}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(org_id="org_123", owner_email="@example.com")
    assert name
    assert name.strip()


@pytest.mark.asyncio
async def test_generate_name_falls_back_when_clerk_name_is_not_a_string(monkeypatch):
    """A malformed Clerk payload (non-string `name`) must fall back, not raise."""

    async def fake_get_org(org_id):
        return {"id": org_id, "name": ["not", "a", "string"]}

    monkeypatch.setattr(company_identity.clerk_admin, "get_organization", fake_get_org)

    name = await company_identity.generate_company_name(org_id="org_123", owner_email="jane.doe@example.com")
    assert name == "Jane Doe"


# -- the slug a new workspace picks at "Create your workspace" -------------------------


def _slug_row(monkeypatch, row):
    async def fake_get_by_slug(slug):
        return row

    monkeypatch.setattr(company_identity.company_slug_repo, "get_by_slug", fake_get_by_slug)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["", "Acme", "acme co", "acme_co", "-acme", "a" * 33])
async def test_availability_rejects_what_slugify_would_change(monkeypatch, slug):
    _slug_row(monkeypatch, None)
    assert await company_identity.slug_unavailable_reason(slug, user_id="user_1") == "invalid"


@pytest.mark.asyncio
async def test_availability_reports_reserved_taken_and_free(monkeypatch):
    _slug_row(monkeypatch, None)
    assert await company_identity.slug_unavailable_reason("support", user_id="user_1") == "reserved"
    assert await company_identity.slug_unavailable_reason("bench-club", user_id="user_1") is None
    _slug_row(monkeypatch, {"slug": "bench-club", "owner_id": "org_other"})
    assert await company_identity.slug_unavailable_reason("bench-club", user_id="user_1") == "taken"


@pytest.mark.asyncio
async def test_availability_treats_the_users_own_or_a_stale_hold_as_free(monkeypatch):
    import time

    _slug_row(monkeypatch, {"slug": "bench-club", "owner_id": "pending:user_1", "held_at": int(time.time())})
    assert await company_identity.slug_unavailable_reason("bench-club", user_id="user_1") is None
    assert await company_identity.slug_unavailable_reason("bench-club", user_id="user_2") == "taken"
    _slug_row(monkeypatch, {"slug": "bench-club", "owner_id": "pending:user_1", "held_at": 0})
    assert await company_identity.slug_unavailable_reason("bench-club", user_id="user_2") is None


@pytest.mark.parametrize("host", ["bv", "bv-dev", "clerk", "accounts", "clkmail", "about"])
def test_product_hosts_on_the_mail_domain_are_reserved(host):
    # The product now lives on the agent-mail domain: a tenant slug equal to a
    # product host would lose inbound mail when that host's record is created.
    assert host in company_identity.RESERVED_SLUGS
