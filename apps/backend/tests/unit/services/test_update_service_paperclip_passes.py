"""Tests for the update_service scheduled worker's workspace purge pass and loop cadence.

``WorkspaceRepo`` (the META store) is patched so no test needs moto. ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import update_service


def _repo(due_rows=None, **kw) -> MagicMock:
    repo = MagicMock()
    repo.scan_purge_due = kw.get("scan") or AsyncMock(return_value=due_rows or [])
    repo.delete = kw.get("delete") or AsyncMock()
    return repo


def _patched(repo):
    return patch("core.repositories.workspace_repo.WorkspaceRepo", MagicMock(return_value=repo))


async def test_purge_pass_no_due_rows_is_noop():
    repo = _repo()
    with _patched(repo):
        await update_service._paperclip_purge_pass()
    repo.scan_purge_due.assert_awaited_once()
    repo.delete.assert_not_awaited()


async def test_purge_pass_deletes_each_due_row_and_swallows_per_row_failure():
    rows = [MagicMock(org_id="org_a"), MagicMock(org_id="org_b"), MagicMock(org_id="org_c")]
    repo = _repo(rows, delete=AsyncMock(side_effect=[RuntimeError("boom"), None, None]))
    with _patched(repo):
        await update_service._paperclip_purge_pass()  # must not raise
    assert [c.args[0] for c in repo.delete.await_args_list] == ["org_a", "org_b", "org_c"]


async def test_purge_pass_scan_failure_does_not_raise():
    repo = _repo(scan=AsyncMock(side_effect=RuntimeError("ddb down")))
    with _patched(repo):
        await update_service._paperclip_purge_pass()
    repo.delete.assert_not_awaited()


async def test_run_scheduled_worker_invokes_the_purge_pass(monkeypatch):
    """Single-iteration smoke test: the worker loop calls the purge pass
    on its first iteration.

    We patch out the interruptible inter-iteration wait (``asyncio.wait_for``) to break the
    loop on the first call. The fourth pass it used to also drive -- applying due Track-2
    container updates (image rolls, resizes, task-definition refreshes) and recovering their
    stranded claims -- went with the ECS runtime.
    """
    purge_calls = 0

    async def fake_purge():
        nonlocal purge_calls
        purge_calls += 1

    class _StopLoop(Exception):
        pass

    async def stop_after_first(*_args, **_kwargs):
        raise _StopLoop()

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_rollup_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=True))
    monkeypatch.setattr(update_service.asyncio, "wait_for", stop_after_first)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert purge_calls == 1, "purge pass runs when admitted (cadence lives on the admission row now)"


async def test_run_scheduled_worker_skips_purge_within_24h(monkeypatch):
    """On the *second* iteration purge should NOT run: admission refuses it.

    Cadence now lives on the try_claim_pass row, not a process clock -- stub
    it to admit the purge pass once, then refuse it, while every other pass
    is admitted every iteration.
    """
    purge_calls = 0
    wait_count = 0

    async def fake_purge():
        nonlocal purge_calls
        purge_calls += 1

    class _StopLoop(Exception):
        pass

    async def fake_wait(*_args, **_kwargs):
        nonlocal wait_count
        wait_count += 1
        if wait_count >= 2:
            # Stop after second iteration completes.
            raise _StopLoop()

    # First check admits the purge pass; the second (same 24h window) refuses it.
    purge_admissions = iter([True, False])

    async def fake_try_claim_pass(name, *, interval_s):
        if name == "paperclip_purge":
            return next(purge_admissions)
        return True  # every other pass wins every tick

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_rollup_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", fake_try_claim_pass)
    monkeypatch.setattr(update_service.asyncio, "wait_for", fake_wait)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert purge_calls == 1, "purge pass runs only when admission wins its window"


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
    monkeypatch.setattr(update_service, "_paperclip_purge_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", fake_try_claim_pass)
    monkeypatch.setattr(update_service.asyncio, "wait_for", fake_wait)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert rollup_calls == 2, "rollup pass: first admitted iteration + after admission wins again"


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
