"""agent_email: slug-derived address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_address_is_agent_at_company_subdomain():
    """Each agent is a mailbox on its own company subdomain —
    {agent}@{slug}.{domain}."""
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    assert addr == "friday@acme-robotics.isol8.co"


def test_different_agent_name_yields_different_local_part():
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Atlas", domain="isol8.co")
    assert addr == "atlas@acme-robotics.isol8.co"


def test_agent_name_is_slugified_into_the_local_part():
    addr = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Chief Ops", domain="isol8.co")
    assert addr == "chief-ops@acme-robotics.isol8.co"


def test_address_is_lowercased_and_trimmed():
    addr = agent_email.build_agent_email_address(slug=" Acme ", agent_name="  Chief  Ops!! ", domain="ISOL8.co")
    assert addr == "chief-ops@acme.isol8.co"


def test_address_is_deterministic():
    kwargs = dict(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    assert agent_email.build_agent_email_address(**kwargs) == (agent_email.build_agent_email_address(**kwargs))


def test_two_agents_of_the_same_company_get_different_addresses():
    """Each agent is its own mailbox on the company subdomain, so two agents
    of the same owner trivially never collide — no truncation/disambiguation
    machinery is needed (the prior per-agent address-collision bug is gone
    with the machinery that caused it)."""
    friday = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Friday", domain="isol8.co")
    atlas = agent_email.build_agent_email_address(slug="acme-robotics", agent_name="Atlas", domain="isol8.co")
    assert friday != atlas
    assert friday == "friday@acme-robotics.isol8.co"
    assert atlas == "atlas@acme-robotics.isol8.co"


@pytest.mark.asyncio
async def test_mint_skips_when_domain_unset(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_skips_when_slug_missing(monkeypatch):
    """No slug means no address — an owner whose slug allocation failed."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    calls = []

    async def fake_put(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="",
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_mint_creates_row_with_owner_as_verified_sender(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return None

    async def fake_put(**kwargs):
        puts.append(kwargs)
        return kwargs

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )

    assert address == "friday@acme.isol8.co"
    assert len(puts) == 1
    assert puts[0]["owner_id"] == "user_1"
    assert puts[0]["agent_id"] == "agent_friday"
    assert puts[0]["verified_senders"] == ["o@example.com"]


@pytest.mark.asyncio
async def test_mint_is_idempotent_when_row_exists_for_the_same_owner(monkeypatch):
    """Re-minting your own address is a no-op re-affirmation, not a write."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_1"}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    address = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert address == "friday@acme.isol8.co"
    assert puts == []


@pytest.mark.asyncio
async def test_mint_refuses_and_logs_when_address_held_by_a_different_owner(monkeypatch, caplog):
    """A collision must be LOUD, not a silent success — the caller must not
    believe minting worked when it wrote nothing."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address, "owner_id": "user_other"}

    async def fake_put(**kwargs):
        puts.append(kwargs)

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    with caplog.at_level("ERROR"):
        result = await agent_email.mint_agent_email_address(
            owner_id="user_1",
            agent_id="agent_friday",
            agent_name="Friday",
            owner_email="o@example.com",
            slug="acme",
        )

    assert result is None
    assert puts == []
    assert any("collision" in r.message for r in caplog.records)
    assert any("user_1" in r.message and "user_other" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mint_returns_none_when_conditional_put_loses_a_race(monkeypatch):
    """`agent_email_repo.put` returning None (lost the conditional put race)
    must be treated as a failed mint, not surfaced as a minted address."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    async def fake_get(address):
        return None

    async def fake_put(**kwargs):
        return None

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", fake_get)
    monkeypatch.setattr(agent_email.agent_email_repo, "put", fake_put)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_repo_failure(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    async def boom(*args, **kwargs):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(agent_email.agent_email_repo, "get_by_address", boom)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_bad_agent_name(monkeypatch):
    """`agent_name` is always dereferenced via `_agent_slug` now (there is no
    more primary/secondary distinction) — a bad `agent_name` (e.g. `None`)
    must not blow up minting, just fail it."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_atlas",
        agent_name=None,
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_never_raises_on_non_string_domain(monkeypatch):
    """`AGENT_EMAIL_DOMAIN` misconfigured as a non-string (e.g. an int from a
    bad env parse) must not blow up the pre-try domain read."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", 12345)

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name="Friday",
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None


from core.services.agent_email import sender_allowed  # noqa: E402


def test_sender_allowed_exact_and_domain():
    row = {"verified_senders": ["owner@example.com", "@clientco.com"]}
    assert sender_allowed(row, "owner@example.com")
    assert sender_allowed(row, "Owner@Example.com")
    assert sender_allowed(row, "anyone@clientco.com")
    assert not sender_allowed(row, "anyone@notclientco.com")
    assert not sender_allowed(row, "anyone@sub.clientco.com")
    assert not sender_allowed({}, "owner@example.com")
