from unittest.mock import AsyncMock

import pytest

from core.repositories import agent_email_repo
from core.services import agent_email


@pytest.mark.asyncio
async def test_team_address_is_agents_at_the_company_subdomain(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email.company_slug_repo, "get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    assert await agent_email.team_address("org_1") == "agents@acme.thelightbulb.company"


@pytest.mark.asyncio
async def test_team_address_none_without_domain_or_slug(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "")
    monkeypatch.setattr(agent_email.company_slug_repo, "get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    assert await agent_email.team_address("org_1") is None
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email.company_slug_repo, "get_by_owner", AsyncMock(return_value=None))
    assert await agent_email.team_address("org_1") is None


@pytest.mark.asyncio
async def test_is_team_address_resolves_slug_owner(monkeypatch):
    monkeypatch.setattr(agent_email_repo.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
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
async def test_a_new_team_row_inherits_the_strictest_legacy_settings(monkeypatch):
    monkeypatch.setattr(agent_email.settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    monkeypatch.setattr(agent_email.company_slug_repo, "get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    legacy = [
        {"address": "friday@acme.thelightbulb.company", "send_to_anyone": True, "verified_senders": ["a@x.com"]},
        {"address": "atlas@acme.thelightbulb.company", "send_to_anyone": False, "outbound_disabled": "complaint"},
    ]
    monkeypatch.setattr(agent_email_repo, "get_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_email_repo, "query_by_owner", AsyncMock(return_value=legacy))
    put = AsyncMock(side_effect=lambda **kw: kw)
    monkeypatch.setattr(agent_email_repo, "put", put)
    assert await agent_email.ensure_team_address("org_1", "owner@x.com") == ("agents@acme.thelightbulb.company", "ok")
    put.assert_awaited_once_with(
        address="agents@acme.thelightbulb.company",
        owner_id="org_1",
        send_to_anyone=False,
        verified_senders=["a@x.com", "owner@x.com"],
        outbound_disabled="complaint",
    )
