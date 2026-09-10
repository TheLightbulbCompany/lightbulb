import asyncio

import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_sets_flag_and_is_bounded(monkeypatch):
    import main
    from core.middleware import shutdown_aware

    shutdown_aware.set_shutting_down(False)

    # Stub the heavy startup/shutdown collaborators.
    monkeypatch.setattr(main, "startup_containers", lambda: asyncio.sleep(0))
    monkeypatch.setattr(main, "_resume_wake_resize_transitions", lambda: asyncio.sleep(0))
    # The one-shot `_reconcile_trial_idle_containers` startup call was replaced
    # by the durable `run_trial_idle_sweeper(stop)` background task (imported
    # locally inside `lifespan`), so it's stubbed at its source module instead
    # of on `main`.
    monkeypatch.setattr("core.services.durable_activity.run_trial_idle_sweeper", lambda stop: asyncio.sleep(0))
    monkeypatch.setattr(main, "run_scheduled_worker", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_running_count_gauge_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_build_teams_event_broker", lambda: asyncio.sleep(0, result=None))

    flag_during_shutdown = {}

    async def _spy_shutdown_containers():
        flag_during_shutdown["set"] = shutdown_aware.is_shutting_down()

    monkeypatch.setattr(main, "shutdown_containers", _spy_shutdown_containers)

    async with main.lifespan(main.app):
        assert shutdown_aware.is_shutting_down() is False
    # After exit, shutdown ran: flag was set, and shutdown_containers saw it.
    assert flag_during_shutdown.get("set") is True
