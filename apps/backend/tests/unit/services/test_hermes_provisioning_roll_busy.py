"""Hazard 2 of the local-exec persistence report: a roll must not stop a warm session mid-turn.

The bridge uploads a turn's filesystem writes to S3 only at the END of the turn
(``bridge/sync.save_profile``), so a forced ``StopRuntimeSession`` loses everything that turn
wrote -- an in-progress clone, an uncommitted ``git add``, a test run's output. Now that
``terminal.backend: local`` runs that work inside the microVM it is real work, not scratch.
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from core.repositories import container_repo
from core.services import container_rpc, hermes_provisioning, runtime_transport
from core.services.messaging import ApiProblem

from tests.unit.services.test_hermes_provisioning_roll import _wire


@pytest.fixture(autouse=True)
def _no_billing_row(monkeypatch):
    """A roll resolves the provider from billing; default to no row (bedrock_claude)."""
    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=None))


#: Captured at import, before any test's ``_wire`` replaces it: the S1-b test needs the REAL
#: fold (transport error -> chained ApiProblem), not the stub every other test here installs.
_REAL_CONTAINER_RPC_CALL = container_rpc.call


def _health(monkeypatch, *answers):
    """Stand in for ``container_rpc.call``, answering one queued ``health`` payload per call;
    the last answer repeats. An answer that is an exception INSTANCE is raised instead."""
    queue, probes = list(answers), []

    async def call(owner_id, method, params=None, **kwargs):
        assert method == "health" and params == {"drain": True}  # idle bridge holds new turns
        probes.append(owner_id)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(container_rpc, "call", call)
    monkeypatch.setattr(hermes_provisioning, "ROLL_IDLE_POLL_S", 0.0)
    return probes


@pytest.mark.asyncio
async def test_roll_waits_for_an_in_flight_turn_before_stopping_the_session(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, {"busy": 1}, {"busy": 2}, {"busy": 0})

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "stop"]
    assert probes == ["org_a"] * 3


@pytest.mark.asyncio
async def test_roll_leaves_a_still_busy_session_running(monkeypatch):
    """A turn still running at the deadline is not a licence to kill it: the runtime is already
    on the new image, and the session picks it up when it next starts."""
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, {"busy": 1})
    monkeypatch.setattr(hermes_provisioning, "ROLL_IDLE_WAIT_S", 0.0)

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated-session-busy"
    assert [c[0] for c in calls] == ["update", "ready"]  # no stop
    assert probes == ["org_a"]


@pytest.mark.asyncio
async def test_an_unreachable_runtime_is_not_a_running_turn(monkeypatch):
    """``ApiProblem`` is the transport's real answer for "no reachable runtime" -- the common
    case, and ``_stop_runtime_session``'s own tolerance for it must not become a ten-minute
    stall on every parked owner."""
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, ApiProblem("RUNTIME_UNAVAILABLE", "gone", retryable=True))

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "stop"]
    assert probes == ["org_a"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raised",
    [asyncio.TimeoutError("health timed out"), RuntimeError('{"code": "NOT_READY"}')],
    ids=["timeout", "gateway_error"],
)
async def test_a_slow_or_failing_probe_is_never_read_as_idle(monkeypatch, raised):
    """Fix round 1 (S1): this used to fail OPEN. A CPU-starved microVM answers /health slowly
    precisely BECAUSE a turn is hammering it (the 2026-09-14 founder-container freeze shape), so
    treating a timeout as "idle" would authorise the exact mid-turn stop this guard prevents."""
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, raised)
    monkeypatch.setattr(hermes_provisioning, "ROLL_IDLE_WAIT_S", 0.0)

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated-session-busy"
    assert [c[0] for c in calls] == ["update", "ready"]  # no stop
    assert probes == ["org_a"]


@pytest.mark.asyncio
async def test_a_starved_microvm_arrives_as_an_apiproblem_and_is_not_idle(monkeypatch):
    """S1-b. ``runtime_transport._request_agentcore`` normalises EVERY AgentCore timeout to
    ``httpx.ReadTimeout``, and ``container_rpc.call`` folds ``httpx.HTTPError`` into the same
    ``ApiProblem`` it raises for an absent runtime -- so the founder-freeze shape never reaches
    the guard as ``asyncio.TimeoutError``. Only the chained ``__cause__`` separates them.

    Exercised THROUGH that fold, not around it: ``container_rpc.call`` is the real one and only
    ``runtime_transport.request`` is stubbed, so a change to either layer's normalisation fails
    this test rather than silently reopening the hole."""
    calls = _wire(monkeypatch, current_image="x:old")
    monkeypatch.setattr(container_rpc, "call", _REAL_CONTAINER_RPC_CALL)
    monkeypatch.setattr(hermes_provisioning, "ROLL_IDLE_WAIT_S", 0.0)

    # `container_rpc.running_container` reads the containers row directly now (it no longer
    # calls `EcsManager.resolve_running_container` -- deletion PR-1's container_rpc change),
    # so the row to address is mocked at its actual source.
    monkeypatch.setattr(
        container_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "gateway_token": "gw",
                "runtime_host": "agentcore",
                "agentcore_runtime_arn": "arn:rt",
                "status": "running",
            }
        ),
    )

    async def _timed_out(*args, **kwargs):
        raise httpx.ReadTimeout("agentcore invoke exceeded 90s")

    monkeypatch.setattr(runtime_transport, "request", _timed_out)

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated-session-busy"
    assert [c[0] for c in calls] == ["update", "ready"]  # no stop


@pytest.mark.asyncio
async def test_a_busy_owner_fails_the_script(monkeypatch):
    # Busy may mean a moved env with the old profile (env drift or --respec): re-run it.
    from scripts import roll_hermes_runtimes

    async def _busy(owner_id, *, dry_run=False, respec=False, restart_session=False):
        return "updated-session-busy"

    monkeypatch.setattr(roll_hermes_runtimes, "roll_runtime_image", _busy)
    monkeypatch.setattr(roll_hermes_runtimes, "prepare_hermes_workspace", AsyncMock(return_value=False))
    assert await roll_hermes_runtimes.main(["--owner-id", "org_a", "--respec"]) == 1
    assert await roll_hermes_runtimes.main(["--owner-id", "org_a"]) == 1


@pytest.mark.asyncio
async def test_a_respec_mints_a_missing_boot_secret_field_and_restarts_the_session(monkeypatch):
    """A boot-secret field the owner's secret lacks: the roll's prepare writes it (a boot change),
    and the roll then restarts the idle session so the bridge reads it. A dry run writes nothing."""
    from scripts import roll_hermes_runtimes

    prepare = AsyncMock(return_value=True)
    roll = AsyncMock(return_value="updated")
    monkeypatch.setattr(roll_hermes_runtimes, "prepare_hermes_workspace", prepare)
    monkeypatch.setattr(roll_hermes_runtimes, "roll_runtime_image", roll)

    assert await roll_hermes_runtimes.main(["--owner-id", "org_a", "--respec"]) == 0
    prepare.assert_awaited_once_with("org_a")
    assert roll.await_args.kwargs == {"dry_run": False, "respec": True, "restart_session": True}

    prepare.reset_mock()
    assert await roll_hermes_runtimes.main(["--owner-id", "org_a", "--dry-run"]) == 0
    prepare.assert_not_awaited()
    assert roll.await_args.kwargs["restart_session"] is False
