"""The proactive layer (product shape, 5 Nov 2026): first run files 3 ideas, the daily look around and the feed posts
are workspace automations for paying workspaces only, under one daily spend cap. Temporal and billing faked."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core import tasks_schema as ts
from core.repositories import automations_repo as repo
from core.repositories import tasks_repo
from core.services import proactive
from core.temporal.automations import FireInput, fire_automation
from tests.unit.super_agent.automations.test_trigger_fire import env  # noqa: F401

WS = "org_1"
NOW = datetime(2027, 1, 4, 14, 0, tzinfo=timezone.utc)
AT = int(NOW.timestamp() * 1000)


@pytest.fixture
def world(env, monkeypatch):  # noqa: F811
    billing = {"subscription_status": "active"}
    upsert = AsyncMock(return_value="created")
    with (
        patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=True)),
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(side_effect=lambda *a, **k: billing)),
        patch("core.services.automation_schedule.upsert", upsert),
        patch("core.services.proactive._timezone", AsyncMock(return_value="America/New_York")),
        patch("core.services.proactive._brief", AsyncMock(return_value="Tell us about customers.")),
        patch("core.repositories.messaging_repo.suggested_ideas", AsyncMock(return_value=[])),
        patch("core.repositories.messaging_repo.dismissed_idea_times", AsyncMock(return_value={})),
    ):
        yield type("World", (), {"env": env, "billing": billing, "upsert": upsert})()


async def _statuses():
    return {r["scheduleId"]: (r["status"], r.get("pausedReason")) for r in await repo.list_schedules(WS)}


async def test_paying_workspace_gets_both_automations_and_one_first_run(world):
    await proactive.ensure(WS)
    await proactive.ensure(WS)
    assert await _statuses() == {proactive.LOOK_AROUND_ID: ("active", None), proactive.FEED_ID: ("active", None)}
    look = await repo.get_schedule(WS, proactive.LOOK_AROUND_ID)
    assert look["ownerUserId"] == WS and look["budgetPerRunUsd"] == 0.25 and look["tz"] == "America/New_York"
    assert look["cron"] == ["0 7 * * *"] and look["deliverTo"] == "none"
    feed = await repo.get_schedule(WS, proactive.FEED_ID)
    assert len(feed["cron"]) == 3 and "--feed" in feed["policyText"]
    first = [t for t, _ in world.env.enqueued if t.source == "idea"]
    assert len({t.trigger_id for t in first}) == 1  # idempotent: the same trigger both times
    start = first[0].ref["start"]
    assert start["visibility"] == "workspace" and start["budgetUsd"] == 0.5 and "suggest_idea" in start["brief"]


async def test_trial_gets_first_run_only_and_plan_changes_converge(world):
    world.billing["subscription_status"] = "trial_local"
    await proactive.ensure(WS)
    assert await _statuses() == {}
    assert [t.source for t, _ in world.env.enqueued] == ["idea"]
    world.billing["subscription_status"] = "active"
    await proactive.ensure(WS)
    assert set((await _statuses()).values()) == {("active", None)}
    world.billing["subscription_status"] = "canceled"
    await proactive.ensure(WS)
    assert set((await _statuses()).values()) == {("paused_system", "not_paying")}
    # The owner turned the feed off: paying again never turns it back on.
    world.billing["subscription_status"] = "active"
    await proactive.ensure(WS)
    await repo.set_status(WS, proactive.FEED_ID, "paused", reason=None, allowed_from=frozenset({"active"}))
    await proactive.ensure(WS)
    assert (await _statuses())[proactive.FEED_ID] == ("paused", None)


async def test_switch_off_does_nothing(world):
    with patch("core.services.super_agent_flags.flags_for", AsyncMock(return_value=False)):
        await proactive.ensure(WS)
    assert await _statuses() == {} and world.env.enqueued == []


async def test_look_around_fire_is_a_workspace_request_with_the_brief(world):
    await proactive.ensure(WS)
    world.env.enqueued.clear()
    trigger_id = await fire_automation(FireInput(WS, proactive.LOOK_AROUND_ID, "schedule"), AT)
    ((trigger, room_id),) = world.env.enqueued
    assert trigger.trigger_id == trigger_id and room_id is None
    start = trigger.ref["start"]
    assert start["visibility"] == "workspace" and start["budgetUsd"] == 0.25
    assert "Tell us about customers." in start["brief"] and "suggest_idea" in start["brief"]


async def test_not_paying_at_fire_time_skips(world):
    await proactive.ensure(WS)
    world.billing["subscription_status"] = "past_due"
    assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT) == "skipped:not_paying"
    assert (await repo.get_schedule(WS, proactive.FEED_ID))["lastSkip"]["reason"] == "not_paying"


async def _spent_run(task_id: str, cost: float, created_at: str):
    await tasks_repo.create_task(
        WS,
        {"taskId": task_id, "identifier": "LB-1", "status": "queued", "audience": {"kind": "workspace"},
         "lane": "shared", "triggerId": "t", "title": "run"},
    )  # fmt: skip
    tasks_repo._table().update_item(  # noqa: SLF001 -- the writer stamps now; the test needs another day
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(task_id)},
        UpdateExpression="SET createdAt = :c",
        ExpressionAttributeValues={":c": created_at},
    )
    await tasks_repo.put_attempt(
        WS,
        task_id,
        1,
        {"status": "ended", "agentId": "a1", "sessionId": "s1", "startedAt": created_at, "usage": {"costUsd": cost}},
    )


async def test_daily_cap_counts_todays_proactive_runs_and_trims_the_budget(world, monkeypatch):
    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-04")
    await proactive.ensure(WS)
    first_task = proactive.first_run_task_id()
    await _spent_run(first_task, 0.6, "2027-01-03T23:00:00+00:00")
    assert await proactive.spent_today(WS) == 0  # yesterday's spend is not today's
    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-03")
    assert await proactive.spent_today(WS) == pytest.approx(0.6)
    tasks_repo._table().update_item(  # noqa: SLF001
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(first_task)},
        UpdateExpression="SET createdAt = :c",
        ExpressionAttributeValues={":c": "2027-01-04T01:00:00+00:00"},
    )
    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-04")
    world.env.enqueued.clear()
    await fire_automation(FireInput(WS, proactive.LOOK_AROUND_ID, "schedule"), AT)
    ((trigger, _),) = world.env.enqueued
    assert trigger.ref["start"]["budgetUsd"] == 0.25  # 0.40 left today: the run's own cap holds
    await _spent_run(str(uuid.uuid4()), 5.0, "2027-01-04T02:00:00+00:00")  # a person's request is not proactive
    run = trigger.ref["taskId"]
    await repo.close_run(WS, proactive.LOOK_AROUND_ID, run, failed=False)
    await _spent_run(run, 0.3, "2027-01-04T07:00:00+00:00")
    assert await proactive.spent_today(WS) == pytest.approx(0.9)
    world.env.enqueued.clear()
    await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT)
    ((trigger, _),) = world.env.enqueued
    assert trigger.ref["start"]["budgetUsd"] == pytest.approx(0.1)  # what is left of today's cap
    await repo.close_run(WS, proactive.FEED_ID, trigger.ref["taskId"], failed=False)
    await _spent_run(trigger.ref["taskId"], 0.2, "2027-01-04T08:00:00+00:00")
    later = AT + 5 * 3_600_000
    assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), later) == "skipped:daily_cap"


def test_trigger_source_idea_is_in_the_contract():
    assert ts.trigger_id("idea", "first-run") == "idea:first-run"


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
