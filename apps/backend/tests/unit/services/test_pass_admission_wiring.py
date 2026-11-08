import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock
from core.services import durable_activity, run_ledger, update_service


@pytest.mark.asyncio
async def test_worker_runs_no_pass_body_when_admission_refuses(monkeypatch):
    """The whole point: at N=2 the loser scans nothing."""
    ran = []
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    for fn in ("_paperclip_purge_pass", "_rollup_pass"):
        monkeypatch.setattr(update_service, fn, AsyncMock(side_effect=lambda _f=fn: ran.append(_f)))
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ran == []


@pytest.mark.asyncio
async def test_worker_runs_each_pass_once_when_admitted(monkeypatch):
    ran = []
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=True))
    for fn in ("_paperclip_purge_pass", "_rollup_pass"):
        monkeypatch.setattr(update_service, fn, AsyncMock(side_effect=lambda _f=fn: ran.append(_f)))
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ran == ["_paperclip_purge_pass", "_rollup_pass"]


@pytest.mark.asyncio
async def test_purge_and_rollup_ask_for_their_documented_windows(monkeypatch):
    claims = []
    monkeypatch.setattr(
        update_service.container_repo,
        "try_claim_pass",
        AsyncMock(side_effect=lambda name, *, interval_s: claims.append((name, interval_s)) or False),
    )
    task = asyncio.create_task(update_service.run_scheduled_worker())
    await asyncio.sleep(0)
    update_service.request_worker_stop()
    await task
    assert ("paperclip_purge", update_service._PURGE_PASS_INTERVAL_SECONDS) in claims
    assert ("owner_day_rollup", update_service._ROLLUP_PASS_INTERVAL_SECONDS) in claims
    # Cadence belongs to the ROW now: no monotonic sentinel may survive in the worker.
    assert "monotonic" not in inspect.getsource(update_service.run_scheduled_worker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mod, sweeper_name, once_name",
    [
        (durable_activity, "run_trial_idle_sweeper", "sweep_trial_idle_once"),
        (run_ledger, "run_ledger_sweeper", "sweep_runs_once"),
    ],
)
async def test_sweepers_skip_their_scan_when_admission_refuses(monkeypatch, mod, sweeper_name, once_name):
    monkeypatch.setattr(mod.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, once_name, AsyncMock(side_effect=AssertionError("scanned while not admitted")))
    stop = asyncio.Event()
    task = asyncio.create_task(getattr(mod, sweeper_name)(stop))
    await asyncio.sleep(0)
    stop.set()
    await task


@pytest.mark.asyncio
async def test_admission_emits_won_and_lost(monkeypatch):
    seen = []
    monkeypatch.setattr("core.services.run_ledger.put_metric", lambda n, **kw: seen.append((n, kw.get("dimensions"))))
    monkeypatch.setattr(run_ledger.container_repo, "try_claim_pass", AsyncMock(return_value=False))
    stop = asyncio.Event()
    task = asyncio.create_task(run_ledger.run_ledger_sweeper(stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    assert ("lifecycle.pass.claimed", {"pass": "run_ledger", "outcome": "lost"}) in seen


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
