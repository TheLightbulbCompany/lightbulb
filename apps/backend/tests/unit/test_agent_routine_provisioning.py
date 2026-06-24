"""Tests for the dormant-routine provisioning helper."""

from unittest.mock import AsyncMock

import pytest

from core.services.agent_routine_provisioning import (
    build_dormant_trigger_body,
    create_agent_routines,
)


def test_dormant_trigger_body_is_always_disabled():
    body = build_dormant_trigger_body("0 6 * * *")
    assert body == {
        "kind": "schedule",
        "cronExpression": "0 6 * * *",
        "timezone": "UTC",
        "enabled": False,
    }
    # The cost-safe invariant: never enabled.
    assert body["enabled"] is False


def test_dormant_trigger_body_custom_timezone():
    body = build_dormant_trigger_body("0 6 * * *", timezone="America/New_York")
    assert body["timezone"] == "America/New_York"
    assert body["enabled"] is False


def _admin_with_existing(existing):
    admin = AsyncMock()
    admin.list_routines = AsyncMock(return_value=existing)
    # create_routine returns an id derived from the title for assertability.
    admin.create_routine = AsyncMock(side_effect=lambda **kw: {"id": f"rid-{kw['body']['title']}"})
    admin.create_routine_trigger = AsyncMock(return_value={"ok": True})
    return admin


@pytest.mark.asyncio
async def test_create_agent_routines_creates_routine_then_dormant_trigger():
    admin = _admin_with_existing([])
    created = await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[{"title": "Morning sweep", "description": "do it", "cron": "0 6 * * *"}],
    )

    assert created == ["rid-Morning sweep"]

    # Routine body carries the cost-safe policies and NO cron.
    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert routine_body == {
        "title": "Morning sweep",
        "description": "do it",
        "assigneeAgentId": "agent-uuid",
        "status": "active",
        "concurrencyPolicy": "coalesce_if_active",
        "catchUpPolicy": "skip_missed",
    }
    assert "cron" not in routine_body

    # Trigger is DORMANT.
    trig = admin.create_routine_trigger.await_args.kwargs
    assert trig["routine_id"] == "rid-Morning sweep"
    assert trig["body"]["enabled"] is False
    assert trig["body"]["cronExpression"] == "0 6 * * *"


@pytest.mark.asyncio
async def test_create_agent_routines_is_idempotent_on_title_match():
    """A routine already assigned to the agent with the same title is skipped."""
    existing = [
        {"id": "old", "assigneeAgentId": "agent-uuid", "title": "Morning sweep"},
    ]
    admin = _admin_with_existing(existing)
    created = await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[
            {"title": "Morning sweep", "description": "d", "cron": "0 6 * * *"},
            {"title": "Weekly report", "description": "d", "cron": "0 8 * * 1"},
        ],
    )
    # Only the second (new) routine is created.
    assert created == ["rid-Weekly report"]
    assert admin.create_routine.await_count == 1
    assert admin.create_routine_trigger.await_count == 1


@pytest.mark.asyncio
async def test_idempotency_ignores_same_title_for_different_agent():
    """Same title but assigned to a DIFFERENT agent must not block creation."""
    existing = [
        {"id": "old", "assigneeAgentId": "other-agent", "title": "Morning sweep"},
    ]
    admin = _admin_with_existing(existing)
    created = await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[{"title": "Morning sweep", "description": "d", "cron": "0 6 * * *"}],
    )
    assert created == ["rid-Morning sweep"]
    assert admin.create_routine.await_count == 1


@pytest.mark.asyncio
async def test_idempotency_handles_enveloped_list_shape():
    """list_routines may return {"routines": [...]} (BFF envelope) or a bare list."""
    existing = {"routines": [{"assigneeAgentId": "agent-uuid", "title": "Morning sweep"}]}
    admin = _admin_with_existing(existing)
    created = await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[{"title": "Morning sweep", "description": "d", "cron": "0 6 * * *"}],
    )
    assert created == []
    assert admin.create_routine.await_count == 0


@pytest.mark.asyncio
async def test_duplicate_spec_in_same_batch_creates_once():
    admin = _admin_with_existing([])
    created = await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[
            {"title": "Morning sweep", "description": "d", "cron": "0 6 * * *"},
            {"title": "Morning sweep", "description": "d", "cron": "0 6 * * *"},
        ],
    )
    assert created == ["rid-Morning sweep"]
    assert admin.create_routine.await_count == 1
