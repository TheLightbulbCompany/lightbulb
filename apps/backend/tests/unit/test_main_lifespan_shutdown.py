import asyncio

import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_sets_flag_and_is_bounded(monkeypatch):
    import main
    from core.middleware import shutdown_aware

    shutdown_aware.set_shutting_down(False)

    # Stub the heavy startup/shutdown collaborators.
    monkeypatch.setattr(main, "_resume_wake_resize_transitions", lambda: asyncio.sleep(0))
    # The one-shot `_reconcile_trial_idle_containers` startup call was replaced
    # by the durable `run_trial_idle_sweeper(stop)` background task (imported
    # locally inside `lifespan`), so it's stubbed at its source module instead
    # of on `main`.
    monkeypatch.setattr("core.services.durable_activity.run_trial_idle_sweeper", lambda stop: asyncio.sleep(0))
    monkeypatch.setattr(main, "run_scheduled_worker", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_running_count_gauge_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_build_teams_event_broker", lambda: asyncio.sleep(0, result=None))

    # `handoff_inflight_provisions` is one of the bounded drain's own cleanup
    # targets, imported locally inside lifespan's shutdown block -- patching
    # its source module reaches that local import. Capturing the flag INSIDE
    # this spy proves set_shutting_down(True) landed BEFORE the drain runs,
    # not just that it's True by the time the whole context manager exits.
    flag_when_drain_ran = {}

    async def _spy_handoff():
        flag_when_drain_ran["set"] = shutdown_aware.is_shutting_down()

    monkeypatch.setattr("core.services.provision_dispatch.handoff_inflight_provisions", _spy_handoff)

    async with main.lifespan(main.app):
        assert shutdown_aware.is_shutting_down() is False
    # After exit, shutdown ran: set_shutting_down(True) is the first thing the
    # shutdown block does (no gateway pool to close any more — Task 9), and
    # the drain-path spy above proves it was already True when the drain ran.
    assert flag_when_drain_ran.get("set") is True
    assert shutdown_aware.is_shutting_down() is True
