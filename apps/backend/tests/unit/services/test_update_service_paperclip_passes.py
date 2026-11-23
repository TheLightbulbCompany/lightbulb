"""Tests for the update_service scheduled worker's loop cadence.

``WorkspaceRepo`` (the META store) is patched so no test needs moto. ``asyncio_mode = "auto"``.
"""

from __future__ import annotations


import pytest

from core.services import update_service


async def test_run_scheduled_worker_rollup_pass_hourly_cadence(monkeypatch):
    """Rollup pass runs when admitted, is skipped when admission refuses it,
    and runs again once admission wins again."""
    rollup_calls = 0

    async def fake_rollup():
        nonlocal rollup_calls
        rollup_calls += 1

    class _StopLoop(Exception):
        pass

    wait_count = 0

    async def fake_wait(*_args, **_kwargs):
        nonlocal wait_count
        wait_count += 1
        if wait_count >= 3:
            raise _StopLoop()

    # Iter 1: admitted -> runs. Iter 2: refused -> skipped. Iter 3: admitted again -> runs.
    rollup_admissions = iter([True, False, True])

    async def fake_try_claim_pass(name, *, interval_s):
        if name == "owner_day_rollup":
            return next(rollup_admissions)
        return True

    monkeypatch.setattr(update_service, "_rollup_pass", fake_rollup)
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", fake_try_claim_pass)
    monkeypatch.setattr(update_service.asyncio, "wait_for", fake_wait)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert rollup_calls == 2, "rollup pass: first admitted iteration + after admission wins again"


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
