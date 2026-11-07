import asyncio

import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_sets_flag_and_is_bounded(monkeypatch):
    import main
    from core.middleware import shutdown_aware

    shutdown_aware.set_shutting_down(False)

    # Stub the heavy startup/shutdown collaborators.
    # The one-shot `_reconcile_trial_idle_containers` startup call was replaced
    # by the durable `run_trial_idle_sweeper(stop)` background task (imported
    # locally inside `lifespan`), so it's stubbed at its source module instead
    # of on `main`.
    monkeypatch.setattr("core.services.durable_activity.run_trial_idle_sweeper", lambda stop: asyncio.sleep(0))
    monkeypatch.setattr(main, "run_scheduled_worker", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(main, "_running_count_gauge_loop", lambda: asyncio.sleep(3600))

    # Provision dispatch holds no per-owner state (no inline runner to hand
    # off since Phase 1), so the bounded drain covers only the worker tasks.
    async with main.lifespan(main.app):
        assert shutdown_aware.is_shutting_down() is False
    # After exit, shutdown ran: set_shutting_down(True) is the first thing the
    # shutdown block does, and the bounded drain completes within budget.
    assert shutdown_aware.is_shutting_down() is True
