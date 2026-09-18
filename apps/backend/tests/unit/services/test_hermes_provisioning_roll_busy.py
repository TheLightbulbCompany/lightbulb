"""Hazard 2 of the local-exec persistence report: a roll must not stop a warm session mid-turn.

The bridge uploads a turn's filesystem writes to S3 only at the END of the turn
(``bridge/sync.save_profile``), so a forced ``StopRuntimeSession`` loses everything that turn
wrote -- an in-progress clone, an uncommitted ``git add``, a test run's output. Now that
``terminal.backend: local`` runs that work inside the microVM it is real work, not scratch.
"""

import pytest

from core.services import container_rpc, hermes_provisioning

from tests.unit.services.test_hermes_provisioning_roll import _wire


def _health(monkeypatch, *answers):
    """Stand in for ``container_rpc.call``, answering one queued ``health`` payload per call;
    the last answer repeats. ``None`` raises -- the bridge is unreachable."""
    queue, probes = list(answers), []

    async def call(owner_id, method, params=None, **kwargs):
        assert method == "health" and kwargs.get("wake") is False
        probes.append(owner_id)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if answer is None:
            raise RuntimeError("no live session")
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
async def test_an_unreachable_bridge_is_not_a_running_turn(monkeypatch):
    """No live session is the common case -- ``_stop_runtime_session``'s own tolerance for it
    must not become a ten-minute stall on every parked owner."""
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, None)

    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "stop"]
    assert probes == ["org_a"]


@pytest.mark.asyncio
async def test_dry_run_never_probes_the_bridge(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old")
    probes = _health(monkeypatch, {"busy": 1})

    assert await hermes_provisioning.roll_runtime_image("org_a", dry_run=True) == "would-update"
    assert calls == [] and probes == []
