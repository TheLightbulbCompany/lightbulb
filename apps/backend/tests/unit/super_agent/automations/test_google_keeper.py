"""GoogleWatchKeeperWorkflow (lane A, Task 8; spec 06 §3.6): renew 48 h before expiry, Continue-As-New, three
failures or a passed expiry pause the automation."""

import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.worker import Worker

from core.repositories import automations_repo as repo
from core.services import automation_hooks
from core.temporal.automations import (
    KEEPER_LEAD,
    GoogleWatchKeeperWorkflow,
    mark_watch_broken,
    renew_google_watch,
)

WS = "org_1"
PID = "abc123def456abc123def456"
DAY_MS = 86_400_000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Fakes:
    """Fake Activities under the real names; `renew` scripts each call's answer."""

    def __init__(self, renew: list):
        self.renew_calls: list[tuple] = []
        self.scheduled: list[datetime] = []  # server (skipped) time each renewal was scheduled
        self.broken: list[tuple] = []
        self._renew = list(renew)

        @activity.defn(name="renew_google_watch")
        async def renew_fake(ws: str, public_id: str) -> int | None:
            self.renew_calls.append((ws, public_id))
            self.scheduled.append(activity.info().scheduled_time)
            answer = self._renew.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        @activity.defn(name="mark_watch_broken")
        async def broken_fake(ws: str, public_id: str) -> None:
            self.broken.append((ws, public_id))

        self.activities = [renew_fake, broken_fake]


async def _run(temporal_env, fakes: Fakes, expires_ms: int, failures: int = 0):
    """Runs the keeper to completion; returns its result and each renewal's offset from the start."""
    queue = f"test-{uuid.uuid4()}"
    start = await temporal_env.get_current_time()
    async with Worker(
        temporal_env.client, task_queue=queue, workflows=[GoogleWatchKeeperWorkflow], activities=fakes.activities
    ):
        result = await temporal_env.client.execute_workflow(
            GoogleWatchKeeperWorkflow.run,
            args=[WS, PID, expires_ms, failures],
            id=f"gwatch:{WS}:{PID}",
            task_queue=queue,
        )
    return result, [t - start for t in fakes.scheduled]


def _about(offsets: list[timedelta], expected: list[timedelta]) -> None:
    assert len(offsets) == len(expected), offsets
    for got, want in zip(offsets, expected):
        assert abs(got - want) < timedelta(minutes=5), (got, want)


async def test_renews_48h_before_expiry(temporal_env):
    now = await temporal_env.get_current_time()
    first_expiry = _ms(now + timedelta(days=7))
    fakes = Fakes([first_expiry + 7 * DAY_MS, None])  # renewed once, then the hook is gone
    result, offsets = await _run(temporal_env, fakes, first_expiry)
    assert result == {"outcome": "gone", "renewals": 1}
    # 5 d to the first renewal (7 d − 48 h); the renewed watch expires at 14 d, so the next is at 12 d.
    _about(offsets, [timedelta(days=5), timedelta(days=12)])
    assert KEEPER_LEAD == timedelta(hours=48)


async def test_keeper_continue_as_new_keeps_ids(temporal_env):
    now = await temporal_env.get_current_time()
    exp = _ms(now + timedelta(days=3))
    fakes = Fakes([exp + 3 * DAY_MS, exp + 6 * DAY_MS, None])
    result, _ = await _run(temporal_env, fakes, exp)
    assert result["renewals"] == 2
    assert fakes.renew_calls == [(WS, PID)] * 3
    assert fakes.broken == []


async def test_three_failures_mark_broken_and_pause(temporal_env):
    now = await temporal_env.get_current_time()
    exp = _ms(now + timedelta(days=7))
    fakes = Fakes([ApplicationError("gog refused", non_retryable=True)] * 3)
    result, offsets = await _run(temporal_env, fakes, exp)
    assert result == {"outcome": "broken", "renewals": 0}
    assert fakes.broken == [(WS, PID)] and len(fakes.renew_calls) == 3
    _about(offsets, [timedelta(days=5), timedelta(days=5, hours=1), timedelta(days=5, hours=2)])


async def test_passed_expiry_marks_broken_without_renewing(temporal_env):
    now = await temporal_env.get_current_time()
    fakes = Fakes([])
    result, _ = await _run(temporal_env, fakes, _ms(now - timedelta(minutes=1)))
    assert result == {"outcome": "broken", "renewals": 0}
    assert fakes.renew_calls == [] and fakes.broken == [(WS, PID)]


async def test_cancelled_on_hook_delete(temporal_env):
    from core.temporal import client as tc

    now = await temporal_env.get_current_time()
    fakes = Fakes([])
    queue = f"test-{uuid.uuid4()}"
    tc.set_client(temporal_env.client)
    try:
        async with Worker(
            temporal_env.client, task_queue=queue, workflows=[GoogleWatchKeeperWorkflow], activities=fakes.activities
        ):
            await automation_hooks.start_keeper(WS, PID, _ms(now + timedelta(days=7)), task_queue=queue)
            await automation_hooks.start_keeper(WS, PID, _ms(now + timedelta(days=7)), task_queue=queue)  # idempotent
            await automation_hooks.cancel_keeper(WS, PID)
            with pytest.raises(WorkflowFailureError) as exc:
                await temporal_env.client.get_workflow_handle(f"gwatch:{WS}:{PID}").result()
            assert isinstance(exc.value.cause, CancelledError)
        await automation_hooks.cancel_keeper(WS, "0" * 24)  # no keeper: fine
    finally:
        tc.set_client(None)
    assert fakes.renew_calls == []


# --- the Activities -------------------------------------------------------------------------------


def _hook(**over):
    return {
        "publicId": PID,
        "scheduleId": "s1",
        "provider": "google",
        "events": ["calendar.changed"],
        "signingMode": "bearer",
        "secretRef": f"hook-{PID}",
        "armed": True,
        "account": "ada@example.com",
        "resource": "ada@example.com",
        "channelId": "ch-old",
        "resourceId": "res-old",
        "expiresAt": "2027-01-01T00:00:00+00:00",
        "pageToken": "sync-token-from-S7",
    } | over


@pytest.fixture
def gog(table):
    run_gog = MagicMock(
        return_value=json.dumps({"kind": "api#channel", "resourceId": "res-new", "expiration": "1799999999000"})
    )
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run_gog),
        patch(
            "core.services.credentials.integration_secret_store.get",
            AsyncMock(return_value={"secret": "tok-1", "mode": "bearer"}),
        ),
    ):
        yield run_gog


async def test_renew_opens_a_new_channel_stops_the_old_and_keeps_the_sync_token(gog):
    await repo.put_hook(WS, _hook())
    assert await renew_google_watch(WS, PID) == 1799999999000
    row = await repo.get_hook(WS, PID)
    assert row["resourceId"] == "res-new" and row["channelId"] != "ch-old"
    assert row["expiresAt"] == "2027-01-15T07:59:59+00:00"
    assert row["pageToken"] == "sync-token-from-S7"
    calls = [c.args[0] for c in gog.call_args_list]
    assert calls[0][:6] == ["gog", "api", "call", "calendar", "v3", "events.watch"]
    assert json.loads(calls[0][9])["token"] == "tok-1"
    assert calls[1][:6] == ["gog", "api", "call", "calendar", "v3", "channels.stop"] and "res-old" in calls[1][7]


async def test_renew_gmail_rearms_the_topic(gog):
    gog.return_value = json.dumps({"historyId": "9", "expiration": "1799999999000"})
    await repo.put_hook(
        WS, _hook(signingMode="google_oidc", events=["gmail.message"], topic="projects/p/topics/t", channelId=None)
    )
    assert await renew_google_watch(WS, PID) == 1799999999000
    assert gog.call_args.args[0][:6] == ["gog", "gmail", "watch", "start", "--topic", "projects/p/topics/t"]


async def test_renew_hook_gone_returns_none(gog):
    assert await renew_google_watch(WS, PID) is None
    assert not gog.called


async def test_renew_failure_raises(gog):
    gog.side_effect = RuntimeError("gog: insufficient scope")
    await repo.put_hook(WS, _hook())
    with pytest.raises(RuntimeError):
        await renew_google_watch(WS, PID)


async def test_mark_watch_broken_pauses_the_automation(table):
    await repo.create_schedule(
        WS, {"scheduleId": "s1", "name": "n", "ownerUserId": "u", "cron": [], "tz": "UTC", "policyText": "p"}
    )
    await repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"draft"}))
    await repo.put_hook(WS, _hook())
    with patch("core.services.automation_schedule.upsert", AsyncMock(return_value=None)) as upsert:
        await mark_watch_broken(WS, PID)
    row = await repo.get_schedule(WS, "s1")
    assert (row["status"], row["pausedReason"]) == ("paused_system", "google_watch_broken")
    upsert.assert_awaited_once()
    await mark_watch_broken(WS, PID)  # already paused: no error
    await mark_watch_broken(WS, "0" * 24)  # hook gone: no error
