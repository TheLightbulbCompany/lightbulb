"""agent_email: slug-derived address building + best-effort idempotent mint."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_email  # noqa: E402


def test_primary_address_is_just_the_slug():
    """The company's main address is its CEO's — {slug}@domain."""
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True
    )
    assert addr == "acme@agents.isol8.co"


def test_secondary_address_appends_the_agent():
    addr = agent_email.build_agent_email_address(
        slug="acme", agent_name="Atlas", domain="agents.isol8.co", is_primary=False
    )
    assert addr == "acme-atlas@agents.isol8.co"


def test_address_is_lowercased_and_agent_name_slugified():
    addr = agent_email.build_agent_email_address(
        slug="Acme",
        agent_name="  Chief  Ops!! ",
        domain="Agents.Isol8.co",
        is_primary=False,
    )
    assert addr == "acme-chief-ops@agents.isol8.co"


def test_address_is_deterministic():
    kwargs = dict(slug="acme", agent_name="Friday", domain="agents.isol8.co", is_primary=True)
    assert agent_email.build_agent_email_address(**kwargs) == (agent_email.build_agent_email_address(**kwargs))


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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
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
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
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

    assert address == "acme@agents.isol8.co"
    assert len(puts) == 1
    assert puts[0]["owner_id"] == "user_1"
    assert puts[0]["agent_id"] == "agent_friday"
    assert puts[0]["verified_senders"] == ["o@example.com"]


@pytest.mark.asyncio
async def test_mint_is_idempotent_when_row_exists(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")
    puts = []

    async def fake_get(address):
        return {"address": address}

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
    assert address == "acme@agents.isol8.co"
    assert puts == []


@pytest.mark.asyncio
async def test_mint_never_raises_on_repo_failure(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

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
async def test_mint_never_raises_on_bad_input(monkeypatch):
    """Best-effort contract holds even for malformed arguments."""
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "agents.isol8.co")

    result = await agent_email.mint_agent_email_address(
        owner_id="user_1",
        agent_id="agent_friday",
        agent_name=None,
        owner_email="o@example.com",
        slug="acme",
    )
    assert result is None
