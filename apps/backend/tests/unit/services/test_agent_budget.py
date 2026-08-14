"""The per-owner agent budget guard.

The interesting cases are the two directions it can be wrong in: refusing a
create it should have allowed (blocks a paying customer on a Paperclip blip),
and allowing one it should have refused (the limit isn't a limit).
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.services.agent_budget import enforce_agent_budget
from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS


def _patch(monkeypatch, *, plan="standard", count=2, count_raises=None, billing_raises=None):
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(side_effect=billing_raises) if billing_raises else AsyncMock(return_value={"plan": plan}),
    )
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
        AsyncMock(side_effect=count_raises) if count_raises else AsyncMock(return_value=count),
    )


@pytest.mark.asyncio
async def test_under_budget_is_allowed(monkeypatch):
    _patch(monkeypatch, count=COMPANY_STANDARD_PLAN_MAX_AGENTS - 1)
    await enforce_agent_budget("owner")  # must not raise


@pytest.mark.asyncio
async def test_at_the_limit_refuses_the_next_create(monkeypatch):
    """The boundary that matters: AT the limit, one more is over it.

    Guards this off-by-one — ``>`` here instead of ``>=`` would silently permit
    an 11th agent on a 10-agent plan.
    """
    _patch(monkeypatch, count=COMPANY_STANDARD_PLAN_MAX_AGENTS)
    with pytest.raises(HTTPException) as exc:
        await enforce_agent_budget("owner")
    assert exc.value.status_code == 409
    assert "Ultra Pro" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_over_budget_refuses(monkeypatch):
    """Already-over owners (the 2026-08-14 admin org) cannot add more."""
    _patch(monkeypatch, count=COMPANY_STANDARD_PLAN_MAX_AGENTS + 5)
    with pytest.raises(HTTPException):
        await enforce_agent_budget("owner")


@pytest.mark.asyncio
async def test_ultra_pro_is_not_capped(monkeypatch):
    _patch(monkeypatch, plan="ultra_pro", count=COMPANY_STANDARD_PLAN_MAX_AGENTS + 50)
    await enforce_agent_budget("owner")  # must not raise


@pytest.mark.asyncio
async def test_undeterminable_count_fails_open(monkeypatch):
    """0 = Paperclip unreachable, NOT an empty roster.

    Fail open deliberately: blocking every create fleet-wide on a Paperclip
    blip is worse than briefly letting one owner past, and the size floor in
    ``ecs_manager._size_for_owner`` still catches them on the next roll.
    """
    _patch(monkeypatch, count=0)
    await enforce_agent_budget("owner")  # must not raise


@pytest.mark.asyncio
async def test_paperclip_error_fails_open(monkeypatch):
    _patch(monkeypatch, count_raises=RuntimeError("paperclip down"))
    await enforce_agent_budget("owner")  # must not raise


@pytest.mark.asyncio
async def test_billing_error_fails_open(monkeypatch):
    _patch(monkeypatch, billing_raises=RuntimeError("dynamo down"))
    await enforce_agent_budget("owner")  # must not raise


@pytest.mark.asyncio
async def test_missing_billing_row_still_enforces(monkeypatch):
    """No billing row is not a bypass — a rowless owner is not ultra_pro."""
    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.count_paperclip_agents_for_owner",
        AsyncMock(return_value=COMPANY_STANDARD_PLAN_MAX_AGENTS),
    )
    with pytest.raises(HTTPException):
        await enforce_agent_budget("owner")
