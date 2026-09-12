"""Tests for the dormant-routine provisioning helper."""

from unittest.mock import AsyncMock

import pytest

from core.services.agent_routine_provisioning import (
    NIGHTLY_CEO_ROUTINE_CRON,
    NIGHTLY_CEO_ROUTINE_DESCRIPTION,
    NIGHTLY_CEO_ROUTINE_TITLE,
    build_dormant_trigger_body,
    build_enabled_trigger_body,
    create_agent_routines,
    create_enabled_schedule_routine,
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
async def test_idempotency_ignores_archived_routines():
    """An archived routine must not satisfy title-skip.

    delete_routine archives rather than deletes (upstream has no DELETE
    route), so a title match on an archived row would leave the owner
    durably "armed" with a routine that can never fire.
    """
    existing = [
        {
            "id": "old",
            "assigneeAgentId": "agent-uuid",
            "title": "Morning sweep",
            "status": "archived",
        },
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


# ---------------------------------------------------------------------------
# The single ENABLED path — paid nightly CEO routine (issue #1273).
# ---------------------------------------------------------------------------


def test_nightly_routine_description_keeps_the_company_control_contract():
    """The nightly wake authorizes ONE company-control action, never arbitrary
    specialist execution — pin that so the prompt cannot regress."""
    assert "Advance ONE actionable constraint" in NIGHTLY_CEO_ROUTINE_DESCRIPTION
    assert "Never self-assign or execute specialist work" in NIGHTLY_CEO_ROUTINE_DESCRIPTION
    # V3: the wake ALWAYS emails the owner instead of the old silent no-op marker.
    assert "ALWAYS end by emailing the owner" in NIGHTLY_CEO_ROUTINE_DESCRIPTION

    contradictory_phrases = (
        "review the company backlog and self-assign ONE task",
        "Pick ONE task you can complete right now",
        "checkout <issueId>",
    )
    for phrase in contradictory_phrases:
        assert phrase not in NIGHTLY_CEO_ROUTINE_DESCRIPTION


def test_enabled_trigger_body_is_enabled():
    body = build_enabled_trigger_body("0 8 * * *")
    assert body == {
        "kind": "schedule",
        "cronExpression": "0 8 * * *",
        "timezone": "UTC",
        "enabled": True,
    }


def test_dormant_default_still_disabled_regression():
    """The general dormant builder must stay enabled:False — the enabled
    sibling is the ONLY exception and must not have leaked into it."""
    assert build_dormant_trigger_body("0 8 * * *")["enabled"] is False


@pytest.mark.asyncio
async def test_create_enabled_schedule_routine_creates_routine_then_enabled_trigger():
    admin = _admin_with_existing([])
    routine_id = await create_enabled_schedule_routine(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="friday-uuid",
        title=NIGHTLY_CEO_ROUTINE_TITLE,
        description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
        cron=NIGHTLY_CEO_ROUTINE_CRON,
    )
    assert routine_id == f"rid-{NIGHTLY_CEO_ROUTINE_TITLE}"

    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert routine_body["title"] == NIGHTLY_CEO_ROUTINE_TITLE
    assert routine_body["assigneeAgentId"] == "friday-uuid"
    # Same cost-safe coalesce/skip posture as every other routine.
    assert routine_body["concurrencyPolicy"] == "coalesce_if_active"
    assert routine_body["catchUpPolicy"] == "skip_missed"
    assert "cron" not in routine_body

    trig = admin.create_routine_trigger.await_args.kwargs
    assert trig["routine_id"] == f"rid-{NIGHTLY_CEO_ROUTINE_TITLE}"
    assert trig["body"]["enabled"] is True
    assert trig["body"]["cronExpression"] == "0 8 * * *"


@pytest.mark.asyncio
async def test_create_enabled_schedule_routine_title_skip_no_dup_no_reenable():
    """Title already assigned to the agent → return None WITHOUT creating a
    routine OR touching its trigger. This is what keeps a paused routine paused
    across a reprovision (no dup, no re-enable)."""
    existing = [
        {"id": "old", "assigneeAgentId": "friday-uuid", "title": NIGHTLY_CEO_ROUTINE_TITLE},
    ]
    admin = _admin_with_existing(existing)
    routine_id = await create_enabled_schedule_routine(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="friday-uuid",
        title=NIGHTLY_CEO_ROUTINE_TITLE,
        description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
        cron=NIGHTLY_CEO_ROUTINE_CRON,
    )
    assert routine_id is None
    admin.create_routine.assert_not_awaited()
    admin.create_routine_trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_paused_reactivates_the_routine():
    """reconcile_paused=True + an existing PAUSED routine → patch it to active
    (re-arm on re-entitlement) instead of skipping, and create nothing new."""
    existing = [
        {"id": "rid-old", "assigneeAgentId": "friday-uuid", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "paused"}
    ]
    admin = _admin_with_existing(existing)
    routine_id = await create_enabled_schedule_routine(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="friday-uuid",
        title=NIGHTLY_CEO_ROUTINE_TITLE,
        description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
        cron=NIGHTLY_CEO_ROUTINE_CRON,
        reconcile_paused=True,
    )
    assert routine_id == "rid-old"
    admin.patch_routine.assert_awaited_once_with(
        routine_id="rid-old", session_cookie="cookie", body={"status": "active"}
    )
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_paused_default_leaves_paused_alone():
    """Default (reconcile_paused=False): a paused routine STAYS paused — the
    intentional opt-out behavior is preserved for callers that don't opt in."""
    existing = [
        {"id": "rid-old", "assigneeAgentId": "friday-uuid", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "paused"}
    ]
    admin = _admin_with_existing(existing)
    routine_id = await create_enabled_schedule_routine(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="friday-uuid",
        title=NIGHTLY_CEO_ROUTINE_TITLE,
        description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
        cron=NIGHTLY_CEO_ROUTINE_CRON,
    )
    assert routine_id is None
    admin.patch_routine.assert_not_awaited()
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_paused_leaves_active_routine_untouched():
    """reconcile_paused=True + an already-ACTIVE routine → skip (already armed):
    no patch, no create."""
    existing = [
        {"id": "rid-old", "assigneeAgentId": "friday-uuid", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "active"}
    ]
    admin = _admin_with_existing(existing)
    routine_id = await create_enabled_schedule_routine(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="friday-uuid",
        title=NIGHTLY_CEO_ROUTINE_TITLE,
        description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
        cron=NIGHTLY_CEO_ROUTINE_CRON,
        reconcile_paused=True,
    )
    assert routine_id is None
    admin.patch_routine.assert_not_awaited()
    admin.create_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_agent_routines_never_emits_enabled_trigger():
    """The general dormant path must never produce an enabled trigger, even
    with the enabled sibling now present in the module."""
    admin = _admin_with_existing([])
    await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[{"title": "Weekly report", "description": "d", "cron": "0 8 * * 1"}],
    )
    assert admin.create_routine_trigger.await_args.kwargs["body"]["enabled"] is False


@pytest.mark.asyncio
async def test_create_enabled_schedule_routine_deletes_routine_when_trigger_fails():
    """If the trigger creation fails after the routine was created, the
    trigger-less routine is deleted (so a retry doesn't title-skip onto a
    routine that can never fire) and the original error propagates so the caller
    releases the arm claim."""
    admin = _admin_with_existing([])
    admin.create_routine_trigger = AsyncMock(side_effect=RuntimeError("trigger 5xx"))

    with pytest.raises(RuntimeError):
        await create_enabled_schedule_routine(
            admin=admin,
            session_cookie="cookie",
            company_id="co1",
            agent_id="friday-uuid",
            title=NIGHTLY_CEO_ROUTINE_TITLE,
            description=NIGHTLY_CEO_ROUTINE_DESCRIPTION,
            cron=NIGHTLY_CEO_ROUTINE_CRON,
        )

    admin.delete_routine.assert_awaited_once_with(
        routine_id=f"rid-{NIGHTLY_CEO_ROUTINE_TITLE}",
        session_cookie="cookie",
    )


@pytest.mark.asyncio
async def test_create_agent_routines_forwards_variable_definitions():
    """Variable definitions ride the routine body — without them Paperclip
    re-syncs {{NAME}} references as required defaultless variables and the
    dormant schedule trigger 422s (rolled back marketplace deploys in prod)."""
    admin = _admin_with_existing([])
    variables = [{"name": "MONTHLY_SPEND_CAP_USD", "type": "number", "defaultValue": 2000, "required": True}]
    await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[
            {
                "title": "Finance snapshot",
                "description": "Cap {{MONTHLY_SPEND_CAP_USD}}",
                "cron": "0 7 * * 1",
                "variables": variables,
            }
        ],
    )
    routine_body = admin.create_routine.await_args.kwargs["body"]
    assert routine_body["variables"] == variables


@pytest.mark.asyncio
async def test_create_agent_routines_omits_variables_key_when_absent():
    admin = _admin_with_existing([])
    await create_agent_routines(
        admin=admin,
        session_cookie="cookie",
        company_id="co1",
        agent_id="agent-uuid",
        routines=[{"title": "Morning sweep", "description": "do it", "cron": "0 6 * * *"}],
    )
    assert "variables" not in admin.create_routine.await_args.kwargs["body"]
