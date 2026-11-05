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
    env.container["last_interactive_at"] = AT // 1000 - 3_600  # someone wrote an hour before the fires
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


async def test_trial_gets_first_run_and_feed_and_the_look_around_follows_the_plan(world):
    world.billing["subscription_status"] = "trial_local"
    await proactive.ensure(WS)
    assert await _statuses() == {proactive.FEED_ID: ("active", None)}  # feed posts for everyone
    assert [t.source for t, _ in world.env.enqueued] == ["idea"]
    world.billing["subscription_status"] = "active"
    await proactive.ensure(WS)
    assert set((await _statuses()).values()) == {("active", None)}
    world.billing["subscription_status"] = "trial_local"  # back on the free plan: feed stays, look around stops
    await proactive.ensure(WS)
    assert await _statuses() == {
        proactive.LOOK_AROUND_ID: ("paused_system", "not_paying"),
        proactive.FEED_ID: ("active", None),
    }
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


async def test_not_paying_at_fire_time_skips_the_look_around_but_not_the_feed(world):
    await proactive.ensure(WS)
    world.billing["subscription_status"] = "trial_local"
    assert await fire_automation(FireInput(WS, proactive.LOOK_AROUND_ID, "schedule"), AT) == "skipped:not_paying"
    assert (await repo.get_schedule(WS, proactive.LOOK_AROUND_ID))["lastSkip"]["reason"] == "not_paying"
    assert (await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT)).startswith("sched:")


async def test_reserve_is_atomic_idempotent_per_run_and_capped_per_day(world, monkeypatch):
    import asyncio

    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-04")
    assert await proactive.reserve(WS, "a", 0.5) == 0.5
    assert await proactive.reserve(WS, "a", 0.5) == 0.5  # a retried fire reserves nothing more
    assert await proactive.reserve(WS, "b", 0.75) == pytest.approx(0.5)  # trimmed to what is left
    assert await proactive.reserve(WS, "c", 0.25) == 0
    await proactive.release(WS, "b")
    racing = await asyncio.gather(proactive.reserve(WS, "d", 0.4), proactive.reserve(WS, "e", 0.4))
    assert sum(racing) <= 0.5 + 1e-9 and await proactive.reserved_today(WS) <= proactive.DAILY_CAP_USD + 1e-9
    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-05")  # a new day, a new cap
    assert await proactive.reserve(WS, "c", 0.25) == 0.25


async def test_fires_reserve_their_budget_and_stop_at_the_daily_cap(world, monkeypatch):
    monkeypatch.setattr(proactive, "_today", lambda: "2027-01-04")
    await proactive.ensure(WS)  # the first run holds 0.50
    assert await proactive.reserved_today(WS) == pytest.approx(0.5)
    world.env.enqueued.clear()
    fire = FireInput(WS, proactive.LOOK_AROUND_ID, "schedule")
    first = await fire_automation(fire, AT)
    assert await fire_automation(fire, AT) == first  # an Activity retry: same run, no second reservation
    ((trigger, _), _) = world.env.enqueued
    assert trigger.ref["start"]["budgetUsd"] == 0.25 and await proactive.reserved_today(WS) == pytest.approx(0.75)
    await repo.close_run(WS, proactive.LOOK_AROUND_ID, trigger.ref["taskId"], failed=False)
    # Another fire while one runs is an overlap: it gives its reservation back.
    running = str(uuid.uuid4())
    assert await repo.claim_run(WS, proactive.FEED_ID, running)
    assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT) == "skipped:overlap"
    assert await proactive.reserved_today(WS) == pytest.approx(0.75)
    await repo.close_run(WS, proactive.FEED_ID, running, failed=False)
    world.env.enqueued.clear()
    await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT)
    ((trigger, _),) = world.env.enqueued
    assert trigger.ref["start"]["budgetUsd"] == 0.25
    await repo.close_run(WS, proactive.FEED_ID, trigger.ref["taskId"], failed=False)
    later = AT + 5 * 3_600_000
    assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), later) == "skipped:daily_cap"
    assert (await repo.get_schedule(WS, proactive.FEED_ID))["lastSkip"]["reason"] == "daily_cap"


async def test_first_run_is_the_workspaces_and_the_backfill_skips_a_dormant_workspace(world):
    world.env.container["last_interactive_at"] = AT // 1000 - 4 * 86_400
    with patch("core.services.proactive._now_ms", return_value=AT):
        await proactive.ensure(WS, backfill=True)
        assert [t for t, _ in world.env.enqueued if t.source == "idea"] == []  # dormant: no backfill spend
        world.env.container["last_interactive_at"] = AT // 1000 - 3_600
        await proactive.ensure(WS, backfill=True)
    ((trigger, room),) = [(t, r) for t, r in world.env.enqueued if t.source == "idea"]
    assert trigger.actor_user_id == WS and trigger.lane == "shared" and room is None  # metered as the workspace


async def test_the_backfill_skips_a_workspace_nobody_ever_used(world):
    world.env.container.pop("last_interactive_at")
    world.env.container["created_at"] = "2026-12-01T00:00:00+00:00"  # no message, ever, since a month ago
    with patch("core.services.proactive._now_ms", return_value=AT):
        await proactive.ensure(WS, backfill=True)
        assert [t for t, _ in world.env.enqueued if t.source == "idea"] == []
        with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None)):
            await proactive.ensure(WS, backfill=True)  # no container row at all: dormant too
        assert [t for t, _ in world.env.enqueued if t.source == "idea"] == []


async def test_provisioning_files_the_first_run_before_any_container_exists(world):
    with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None)):
        await proactive.ensure(WS)  # provision / billing path: no idle check
    assert len([t for t, _ in world.env.enqueued if t.source == "idea"]) == 1


async def test_a_never_used_workspace_gets_no_feed_posts(world):
    await proactive.ensure(WS)
    world.env.container.pop("last_interactive_at")
    world.env.container["created_at"] = "2026-12-01T00:00:00+00:00"  # created a month ago, nobody ever wrote
    assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT) == "skipped:idle"
    with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=None)):  # deleted
        assert await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT) == "skipped:idle"
    world.env.container["created_at"] = "2027-01-04T10:00:00+00:00"  # new today: not idle
    assert (await fire_automation(FireInput(WS, proactive.FEED_ID, "schedule"), AT)).startswith("sched:")


async def test_a_canceled_or_expired_workspace_pauses_its_feed_too(world):
    await proactive.ensure(WS)
    world.billing["subscription_status"] = "canceled"
    await proactive.ensure(WS)
    assert set((await _statuses()).values()) == {("paused_system", "not_paying")}
    world.billing["subscription_status"] = "trial_local"  # an unexpired free trial: feed back on
    world.billing["trial_end"] = 4_102_444_800  # 2100
    await proactive.ensure(WS)
    assert (await _statuses())[proactive.FEED_ID] == ("active", None)


async def test_context_never_shows_a_private_request(world, monkeypatch):
    rows = {
        "working": [
            {"title": "Draft my resignation", "audience": {"kind": "person", "userIds": ["user_sam"]}},
            {"title": "Fix the pricing page", "audience": {"kind": "workspace"}},
            {"title": "Plan the launch room", "audience": {"kind": "room", "roomId": "r1"}},
        ],
        "resolved": [{"title": "Private tax return", "audience": {"kind": "person", "userIds": ["user_sam"]}}],
    }

    async def by_status(ws, status, **_):
        return rows.get(status, []), None

    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", by_status)
    text = await proactive.context(WS)
    assert "Fix the pricing page" in text
    assert "resignation" not in text and "tax return" not in text and "launch room" not in text


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
