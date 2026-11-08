"""Retired built-in schedules cannot wake agents; user automations remain intact."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import automations_repo as repo
from core.services import proactive
from core.temporal.automations import FireInput, fire_automation
from tests.unit.super_agent.automations.test_trigger_fire import env  # noqa: F401

WS = "org_1"
AT = 1_799_053_200_000


def row(sid):
    return {
        "scheduleId": sid,
        "ownerUserId": WS,
        "name": sid,
        "cron": ["0 8 * * *"],
        "tz": "UTC",
        "policyText": "Do work",
        "deliverTo": "none",
        "budgetPerRunUsd": 0.25,
    }


@pytest.mark.parametrize("sid", sorted(proactive.RETIRED_SCHEDULE_IDS))
async def test_retired_fire_runs_nothing_and_cleanup_keeps_user_schedules(env, sid):  # noqa: F811
    await repo.create_schedule(WS, row(sid))
    await repo.set_status(WS, sid, "active", reason=None, allowed_from=frozenset({"draft"}))
    await repo.create_schedule(WS, row("user-reminder"))
    # A queued old fire is rejected even before the next cleanup pass.
    assert await fire_automation(FireInput(WS, sid, "schedule"), AT) == "skipped:retired"
    assert env.enqueued == []
    deleted = AsyncMock()
    with patch("core.services.automation_schedule.delete", deleted):
        await proactive.ensure(WS)
        await proactive.ensure(WS)
    deleted.assert_awaited_once_with(WS, sid)
    assert await repo.get_schedule(WS, sid) is None
    assert await repo.get_schedule(WS, "user-reminder") is not None


async def test_fresh_workspace_creates_no_background_schedules(env):  # noqa: F811
    with patch("core.services.automation_schedule.upsert", AsyncMock()) as upsert:
        await proactive.ensure(WS)
    assert await repo.list_schedules(WS) == []
    upsert.assert_not_awaited()


async def test_daily_reconcile_ensures_every_switched_on_workspace(table):
    from core.temporal import automations

    class _Client:
        async def list_schedules(self):
            async def empty():
                return
                yield

            return empty()

    ensure = AsyncMock(side_effect=[RuntimeError("one bad workspace"), None])
    with (
        patch("core.temporal.automations.sweep_allowed", return_value=True),
        patch("core.temporal.client.get_client", return_value=_Client()),
        patch("core.services.super_agent_flags.list_on", AsyncMock(return_value=["org_bad", WS])),
        patch("core.services.proactive.ensure", ensure),
    ):
        out = await automations.reconcile_schedules()
    assert [c.args[0] for c in ensure.await_args_list] == ["org_bad", WS] and out["proactive"] == 1
