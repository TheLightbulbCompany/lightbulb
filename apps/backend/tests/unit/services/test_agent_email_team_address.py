from unittest.mock import AsyncMock

import pytest

from core.repositories import agent_email_repo
from core.services import agent_email


@pytest.mark.asyncio
async def test_team_address_is_agents_at_the_company_subdomain(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email, "slug_for_owner", AsyncMock(return_value="acme"))
    assert await agent_email.team_address("org_1") == "agents@acme.thelightbulb.company"


@pytest.mark.asyncio
async def test_team_address_none_without_domain_or_slug(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    monkeypatch.setattr(agent_email, "slug_for_owner", AsyncMock(return_value="acme"))
    assert await agent_email.team_address("org_1") is None
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email, "slug_for_owner", AsyncMock(return_value=None))
    assert await agent_email.team_address("org_1") is None


@pytest.mark.asyncio
async def test_an_agent_named_agents_cannot_take_the_team_mailbox(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    put = AsyncMock()
    monkeypatch.setattr(agent_email.agent_email_repo, "put", put)
    result = await agent_email.mint_agent_email_address(
        owner_id="org_1", agent_id="a1", agent_name="Agents", owner_email="", slug="acme"
    )
    assert result is None
    put.assert_not_called()


@pytest.mark.asyncio
async def test_is_team_address_resolves_slug_owner(monkeypatch):
    monkeypatch.setattr(
        agent_email_repo.company_slug_repo, "get_by_slug", AsyncMock(return_value={"slug": "acme", "owner_id": "org_1"})
    )
    assert await agent_email_repo.is_team_address("Agents@acme.thelightbulb.company") == ("org_1", "acme")
    assert await agent_email_repo.is_team_address("friday@acme.thelightbulb.company") is None


@pytest.mark.asyncio
async def test_a_team_address_on_another_domain_is_nobody(monkeypatch):
    monkeypatch.setattr(agent_email_repo.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    get = AsyncMock(return_value={"slug": "acme", "owner_id": "org_1"})
    monkeypatch.setattr(agent_email_repo.company_slug_repo, "get_by_slug", get)
    assert await agent_email_repo.is_team_address("agents@acme.evil.com") is None
    monkeypatch.setattr(agent_email_repo.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await agent_email_repo.is_team_address("agents@acme.thelightbulb.company") is None


@pytest.mark.asyncio
async def test_is_team_address_none_when_slug_has_no_owner(monkeypatch):
    monkeypatch.setattr(agent_email_repo.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email_repo.company_slug_repo, "get_by_slug", AsyncMock(return_value=None))
    assert await agent_email_repo.is_team_address("agents@acme.thelightbulb.company") is None


@pytest.mark.asyncio
async def test_ensure_agent_address_refuses_reserved_name(monkeypatch):
    monkeypatch.setattr(agent_email, "resolve_from_address", AsyncMock(return_value=None))
    repo = type("R", (), {"get_onboarding_state": AsyncMock(return_value={"company_id": "c1"})})
    monkeypatch.setattr(agent_email, "PaperclipRepo", lambda **_: repo)
    monkeypatch.setattr(agent_email, "resolve_company_name", AsyncMock(return_value="Acme"))
    monkeypatch.setattr(agent_email, "allocate_company_slug", AsyncMock(return_value="acme"))
    monkeypatch.setattr(agent_email, "resolve_agent_name", AsyncMock(return_value="Agents"))
    assert await agent_email.ensure_agent_address(owner_id="org_1", agent_id="a1", owner_email="") == (
        None,
        "reserved",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["AGENTS", "agents "])
async def test_reserved_names_are_refused_by_mint(monkeypatch, name):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    put = AsyncMock()
    monkeypatch.setattr(agent_email.agent_email_repo, "put", put)
    assert (
        await agent_email.mint_agent_email_address(
            owner_id="org_1", agent_id="a1", agent_name=name, owner_email="", slug="acme"
        )
        is None
    )
    put.assert_not_called()
