"""Tests for core/services/provision_dispatch.py (inline fast path + SFN
backstop — see module docstring)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import provision_dispatch


def _already_exists_error():
    return ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "closed"}},
        "StartExecution",
    )


@pytest.fixture
def sfn(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(provision_dispatch, "_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def clean_inline_registry():
    provision_dispatch._INLINE_ATTEMPTS.clear()
    yield
    provision_dispatch._INLINE_ATTEMPTS.clear()


async def _await_inline(owner_id: str):
    """Await the spawned inline attempt task for this owner (if any)."""
    entry = provision_dispatch._INLINE_ATTEMPTS.get(owner_id)
    if entry:
        await entry[1]


async def test_fresh_dispatch_starts_execution_then_runs_inline(monkeypatch, sfn):
    # Custody-first: the machine is WATCHING this claim before attempt 0
    # exists, so no inline failure mode needs a handoff.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    claim_calls = {}
    order = []

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(owner=owner_id, claim=claim_id, name=execution_name)
        order.append("claimed")
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    sfn.start_execution.side_effect = lambda **kw: order.append("started")

    async def runner(owner_id, claim_id):
        order.append("inline")
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    assert claim_calls["name"] == provision_dispatch.execution_name("user_abc", claim_calls["claim"])
    # Claim, THEN start the watchdog, THEN run attempt 0 in-process.
    assert order == ["claimed", "started", "inline"]
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == provision_dispatch.execution_name("user_abc", claim_calls["claim"])
    assert kwargs["input"] == provision_dispatch.execution_input("user_abc", claim_calls["claim"])
    # The registry entry was popped by the attempt's own finally.
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_branch_b_start_failure_releases_claim_and_skips_inline(monkeypatch, sfn):
    # No watchdog means no durable owner — the claim must not be left held by
    # an unwatched inline attempt. Release it so the next poll re-dispatches.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(claim=claim_id)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )
    await provision_dispatch.dispatch_provision("user_abc")
    runner.assert_not_awaited()
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("user_abc", claim_calls["claim"])
    assert kwargs["last_error"] == "provision dispatch: StartExecution failed"
    assert kwargs["retryable"] is True


async def test_branch_b_closed_execution_releases_claim_and_skips_inline(monkeypatch, sfn):
    # An 8-hex name collision on a FRESH claim: the name embeds claim_id[:8]
    # and a closed execution already holds it, so this claim can never be
    # watched. Release it — the next poll mints a new claim and name.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("user_abc")
    runner.assert_not_awaited()
    release.assert_awaited_once()
    assert release.await_args.kwargs["last_error"] == "provision dispatch: execution name collision"


async def test_execution_input_is_byte_identical_across_calls():
    """Idempotent StartExecution requires this — a re-issue (Branch A) must
    recompute the EXACT same input for the SAME claim_id, every time."""
    first = provision_dispatch.execution_input("user_x", "claim-123")
    second = provision_dispatch.execution_input("user_x", "claim-123")
    assert first == second


async def test_execution_input_command_shape():
    payload = json.loads(provision_dispatch.execution_input("user_x", "claim-123"))
    assert payload["attempt"] == 0
    # `uv run` is load-bearing (no ENTRYPOINT/PATH venv setup in the image);
    # --no-sync keeps the baked --no-dev venv instead of re-downloading the
    # dev-dependency group at every provisioner task start.
    assert payload["command"][:6] == ["uv", "run", "--no-sync", "python", "-m", "scripts.provision_runner"]
    # Attempt 0's fencing token is claim_id itself — deterministic (see
    # execution_input docstring: nothing here may be freshly randomized per
    # call, or Branch-A re-issues would stop being byte-identical).
    assert payload["attempt_token"] == "claim-123"


async def test_claim_lost_starts_nothing(monkeypatch, sfn):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", AsyncMock(return_value=None))
    await provision_dispatch.dispatch_provision("user_abc")
    sfn.start_execution.assert_not_called()


async def test_stamped_running_row_reissues_same_start(monkeypatch, sfn):
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    await provision_dispatch.dispatch_provision("user_abc")
    claim.assert_not_awaited()  # branch A never mints a new claim
    assert sfn.start_execution.call_args.kwargs["name"] == name


async def test_stamped_error_row_reissues_without_new_claim(monkeypatch, sfn):
    # During in-machine retries the row honestly reads error/error_retrying;
    # dispatch MUST still land on branch A (idempotent re-issue), or every ~3s
    # poll mints a second concurrent execution — the duplicate-mint incident.
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "error",
        "substatus": "error_retrying",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    await provision_dispatch.dispatch_provision("user_abc")
    claim.assert_not_awaited()  # branch A never mints a new claim
    assert sfn.start_execution.call_args.kwargs["name"] == name


async def test_stamped_closed_execution_falls_through_to_fresh_inline_claim(monkeypatch, sfn):
    # Branch A "closed" is the design's "execution CLOSED → mint fresh claim"
    # leg: without the fall-through, a provisioning row whose execution closed
    # without a terminal row write is wedged forever.
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(claim=claim_id, name=execution_name)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    # First call = the Branch A re-issue (name held by a CLOSED execution);
    # second = Branch B's watchdog start under the fresh claim.
    sfn.start_execution.side_effect = [_already_exists_error(), None]
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    # Branch B ran: a fresh claim was minted, watched, and attempt 0 ran inline.
    assert claim_calls["claim"] != "claimuuid-1234"
    runner.assert_awaited_once_with("user_abc", claim_calls["claim"])
    assert sfn.start_execution.call_count == 2
    assert sfn.start_execution.call_args.kwargs["name"] == provision_dispatch.execution_name(
        "user_abc", claim_calls["claim"]
    )


async def test_branch_a_transient_failure_leaves_row_untouched(monkeypatch, sfn):
    # A transient StartExecution failure on branch A must NOT release the row
    # to error — the claim is not this dispatcher's to release.
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )
    await provision_dispatch.dispatch_provision("user_abc")
    claim.assert_not_awaited()
    release.assert_not_awaited()


async def test_branch_a_stands_down_while_inline_attempt_live(monkeypatch, sfn):
    # The ~3s poll cadence re-enters dispatch while attempt 0 runs in this
    # process; Branch A must NOT hand the live claim to the state machine.
    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    gate = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        await gate.wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await provision_dispatch.dispatch_provision("user_abc")
    sfn.start_execution.assert_not_called()
    claim.assert_not_awaited()
    gate.set()
    await _await_inline("user_abc")


async def test_inline_failure_exit_code_leaves_row_writes_to_runner(monkeypatch, sfn):
    # Exit 10/11: run() already released the row itself; the dispatcher must
    # not double-write AND must not start a second execution — the watchdog
    # from claim time observes the error row within a tick. This is the
    # regression that broke dev fleet-wide on 2026-08-13: the old code did
    # nothing here and relied on a frontend poll to hand the claim over.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", AsyncMock(return_value=11))
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    release.assert_not_awaited()
    assert sfn.start_execution.call_count == 1  # the claim-time watchdog only
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_inline_crash_releases_claim_to_error(monkeypatch, sfn):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(claim=claim_id)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", AsyncMock(side_effect=RuntimeError("boom")))
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("user_abc", claim_calls["claim"])
    # attempt_token fence: after an SFN takeover re-stamps the token, this
    # release must silently no-op rather than clobber the machine-owned row.
    assert kwargs == {
        "last_error": "inline provision attempt crashed",
        "retryable": True,
        "attempt_token": provision_dispatch.inline_attempt_token(claim_calls["claim"]),
    }


async def test_runner_subprocess_contract(monkeypatch):
    # The child is the backend's own venv interpreter running the runner
    # module, with the DISTINCT inline token on the same env contract the
    # state machine uses (PROVISION_ATTEMPT_TOKEN).
    import sys

    captured = {}

    async def fake_exec(*argv, env=None):
        captured["argv"] = argv
        captured["env"] = env

        class P:
            async def wait(self):
                return 0

        return P()

    monkeypatch.setattr(provision_dispatch.asyncio, "create_subprocess_exec", fake_exec)
    code = await provision_dispatch._runner_subprocess("user_abc", "claim-1")
    assert code == 0
    assert captured["argv"] == (
        sys.executable,
        "-m",
        "scripts.provision_runner",
        "--owner",
        "user_abc",
        "--claim",
        "claim-1",
    )
    assert captured["env"]["PROVISION_ATTEMPT_TOKEN"] == provision_dispatch.inline_attempt_token("claim-1")


async def test_handoff_terminates_children_without_start(monkeypatch, sfn):
    # Shutdown: cancel + await the attempt (the cancel path terminates the
    # child). NO StartExecution — the execution has been running since claim
    # time; it observes the abandoned row on its own clock.
    order = []
    gate = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            order.append("cancelled")
            raise
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await asyncio.sleep(0)  # let the attempt reach its await
    await provision_dispatch.handoff_inflight_provisions()
    assert order == ["cancelled"]
    sfn.start_execution.assert_not_called()
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_handoff_leaves_row_untouched(monkeypatch, sfn):
    # The handoff must never write the row: the claim belongs to the running
    # execution, which is the only thing allowed to move it.
    gate = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        await gate.wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await asyncio.sleep(0)
    await provision_dispatch.handoff_inflight_provisions()  # must not raise
    release.assert_not_awaited()


async def test_inline_timeout_terminates_without_second_start(monkeypatch, sfn):
    # The machine bounds every ECS attempt at 30 min (taskTimeout); attempt 0
    # gets the same ceiling. On expiry: child cancelled (terminated), row
    # untouched, and NO StartExecution — the watchdog's tick backstop owns
    # the takeover.
    monkeypatch.setattr(provision_dispatch, "_INLINE_ATTEMPT_TIMEOUT_S", 0.05)
    cancelled = asyncio.Event()

    async def wedged_runner(owner_id, claim_id):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", wedged_runner)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await _await_inline("user_abc")
    assert cancelled.is_set()
    sfn.start_execution.assert_not_called()
    release.assert_not_awaited()
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_inline_queue_saturation_releases_claim_for_machine(monkeypatch, sfn):
    # Slot drought (the 2026-08-13 Spot-drought pileup): a queued attempt used
    # to hold its claim indefinitely AND suppress Branch A on every poll, so
    # the machine never took over. Now the queue wait is bounded and the row
    # is made honest, which the watchdog observes as error+retryable.
    monkeypatch.setattr(provision_dispatch, "_INLINE_QUEUE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(provision_dispatch, "_INLINE_CONCURRENCY", asyncio.Semaphore(0))
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await _await_inline("user_abc")
    runner.assert_not_awaited()  # never got a slot
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("user_abc", "claimuuid-1234")
    assert kwargs["last_error"] == "inline queue saturated — machine takes over"
    assert kwargs["retryable"] is True
    # Fenced on the claim-time token (try_claim_provision stamps claim_id):
    # a retry that already re-stamped it owns the row, so this no-ops.
    assert kwargs["attempt_token"] == "claimuuid-1234"
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_inline_slot_released_after_attempt(monkeypatch, sfn):
    # The explicit acquire/release (needed for the bounded queue wait) must
    # not leak a permit: three sequential attempts on a 1-slot semaphore all
    # run, which fails if the finally does not release.
    monkeypatch.setattr(provision_dispatch, "_INLINE_QUEUE_TIMEOUT_S", 1)
    monkeypatch.setattr(provision_dispatch, "_INLINE_CONCURRENCY", asyncio.Semaphore(1))
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    for i in range(3):
        provision_dispatch._spawn_inline(f"user_{i}", f"claim-{i}")
        await _await_inline(f"user_{i}")
    assert runner.await_count == 3
