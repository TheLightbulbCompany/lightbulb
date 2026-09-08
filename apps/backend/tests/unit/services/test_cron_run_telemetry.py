"""Tests for the cron-run telemetry sweep (``core/services/cron_run_telemetry.py``).

Covers the three properties the sweep exists for:

- an unattended cron run (no human WS, no operator relay) still produces one
  ``agent_run_completed`` with the same shape the live paths emit;
- a run offered twice — by a live path and by the sweep, or by two consecutive
  sweeps — produces exactly ONE event;
- a ``cron.runs`` failure (unreachable container, rejected RPC) is swallowed and
  never raises into the shared scheduled worker.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import settings
from core.services import cron_run_telemetry, run_analytics

OWNER = "user_owner1"
NOW_MS = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def _analytics_enabled(monkeypatch):
    run_analytics._seen_run_ids.clear()
    run_analytics._run_actor.clear()
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_WRITE_KEY", "phc_test")


def _entry(run_id="cron-run-1", **over):
    """One CronRunLogEntry, shaped as the pinned schema defines it."""
    return {
        "ts": NOW_MS,
        "jobId": "job-abc",
        "action": "finished",
        "status": "ok",
        "completionStatus": "succeeded",
        "sessionKey": "agent:agent_7:cron:job-abc:run:cron-run-1",
        "runId": run_id,
        "runAtMs": NOW_MS - 4000,
        "durationMs": 4000,
        **over,
    }


async def _sweep(entries, *, rpc=None):
    """Run one sweep against a single running container; returns captured events."""
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, "10.0.0.5"))
    oneshot = rpc or AsyncMock(return_value={"entries": entries, "total": len(entries)})
    captured = []

    with (
        patch.object(cron_run_telemetry.container_repo, "get_by_status", AsyncMock(return_value=[{"owner_id": OWNER}])),
        patch.object(cron_run_telemetry, "get_ecs_manager", return_value=ecs),
        patch.object(cron_run_telemetry, "gateway_rpc_oneshot", oneshot),
        patch.object(run_analytics, "capture_product_event", side_effect=lambda **kw: captured.append(kw)),
        patch.object(run_analytics, "_is_internal_owner", AsyncMock(return_value=False)),
        patch.object(run_analytics, "put_metric") as run_metric,
    ):
        await cron_run_telemetry.sweep_cron_runs()
        # Drain the fire-and-forget capture tasks the emitter spawns.
        for task in list(run_analytics.asyncio.all_tasks()):
            if task is not run_analytics.asyncio.current_task():
                await task
    return captured, oneshot, run_metric


# --- an unattended cron run becomes an event -------------------------------


async def test_unattended_cron_run_emits_agent_run_completed():
    captured, oneshot, run_metric = await _sweep([_entry()])

    assert len(captured) == 1
    props = captured[0]["properties"]
    assert captured[0]["event"] == "agent_run_completed"
    assert captured[0]["distinct_id"] == OWNER
    # Same property shape the live paths produce for a cron session.
    assert props["run_source"] == "cron"
    assert props["initiation_type"] == "autonomous"
    assert props["outcome"] == "success"
    assert props["run_id"] == "cron-run-1"
    assert props["agent_id"] == "agent_7"
    assert props["cron_id"] == "job-abc"
    assert props["duration_ms"] == 4000
    # Deterministic capture uuid -> PostHog can drop a cross-process re-offer.
    assert captured[0]["event_uuid"]

    params = oneshot.await_args.kwargs["params"]
    assert oneshot.await_args.kwargs["method"] == "cron.runs"
    assert params["scope"] == "all"
    assert params["statuses"] == ["ok", "error"]
    assert params["limit"] <= 200  # CronRunsParamsSchema cap

    # The CloudWatch run.completed metric is NOT fired from the sweep (no
    # durable dedup, and the worker runs x2 during a rolling deploy).
    assert [c for c in run_metric.call_args_list if c.args and c.args[0] == "run.completed"] == []


async def test_failed_cron_run_maps_to_failure():
    captured, _, _ = await _sweep([_entry(status="error", completionStatus="failed")])
    assert captured[0]["properties"]["outcome"] == "failure"


async def test_cron_origin_wins_over_a_current_target_session_key():
    """A ``target: "current"`` job reuses the webchat session key; the run is
    still autonomous and must not be classified as human."""
    captured, _, _ = await _sweep([_entry(sessionKey="agent:agent_7:main")])
    props = captured[0]["properties"]
    assert props["run_source"] == "cron"
    assert props["initiation_type"] == "autonomous"
    assert props["agent_id"] == "agent_7"


async def test_entries_outside_the_lookback_and_without_a_run_id_are_skipped():
    stale_ms = int((time.time() - cron_run_telemetry._LOOKBACK_S - 60) * 1000)
    captured, _, _ = await _sweep(
        [
            _entry(run_id=None, runId=None),  # no idempotency key -> not emittable
            _entry(run_id="fresh"),
            _entry(run_id="ancient", ts=stale_ms),  # newest-first: ends the page
            _entry(run_id="after-the-break"),
        ]
    )
    assert [c["properties"]["run_id"] for c in captured] == ["fresh"]


# --- seen twice -> one event ------------------------------------------------


async def test_a_run_already_emitted_by_a_live_path_is_not_re_emitted():
    """The reader loop / operator relay saw this run; the sweep must not
    double-count it."""
    live = run_analytics.schedule_run_completed(
        owner_id=OWNER,
        parsed={"agent_id": "agent_7", "source": "cron", "cron_id": "job-abc"},
        payload={"runId": "cron-run-1", "data": {}},
        phase="end",
    )
    assert live is not None
    # _sweep drains every pending capture task, the live one included -- so the
    # captured list is the TOTAL for this run across both emit paths.
    captured, _, run_metric = await _sweep([_entry()])
    assert len(captured) == 1
    assert captured[0]["properties"]["run_id"] == "cron-run-1"
    # The live path already fired run.completed (before this patch was in
    # place); the sweep adds none of its own.
    assert [c for c in run_metric.call_args_list if c.args and c.args[0] == "run.completed"] == []


async def test_two_consecutive_sweeps_of_the_same_page_emit_once():
    first, _, _ = await _sweep([_entry()])
    second, _, _ = await _sweep([_entry()])
    assert len(first) == 1
    assert second == []


# --- failures never reach the worker ---------------------------------------


async def test_cron_runs_rpc_failure_is_swallowed():
    failing = AsyncMock(side_effect=RuntimeError("cron.runs rejected"))
    captured, _, _ = await _sweep([], rpc=failing)
    assert captured == []
    assert failing.await_count == 1


async def test_unreachable_container_is_skipped_without_an_rpc():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    oneshot = AsyncMock()
    with (
        patch.object(cron_run_telemetry.container_repo, "get_by_status", AsyncMock(return_value=[{"owner_id": OWNER}])),
        patch.object(cron_run_telemetry, "get_ecs_manager", return_value=ecs),
        patch.object(cron_run_telemetry, "gateway_rpc_oneshot", oneshot),
    ):
        await cron_run_telemetry.sweep_cron_runs()
    oneshot.assert_not_awaited()


async def test_container_listing_failure_does_not_raise():
    with patch.object(
        cron_run_telemetry.container_repo, "get_by_status", AsyncMock(side_effect=RuntimeError("ddb down"))
    ):
        with pytest.raises(RuntimeError):
            await cron_run_telemetry.sweep_cron_runs()
    # The worker's own try/except is the containment for this one -- assert the
    # loop actually wraps the call rather than pretending the sweep swallows it.
    import inspect

    from core.services import update_service

    src = inspect.getsource(update_service.run_scheduled_worker)
    assert "sweep_cron_runs()" in src
    assert "cron_run_telemetry sweep crashed" in src
