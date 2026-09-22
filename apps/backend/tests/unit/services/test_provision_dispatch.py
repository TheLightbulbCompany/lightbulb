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


@pytest.fixture(autouse=True)
def stub_claim_attempt(monkeypatch):
    """The inline path stamps its fencing token at slot-acquire; stub it so
    tests do not need DynamoDB. Tests that care override this."""
    monkeypatch.setattr(provision_dispatch.container_repo, "claim_attempt", AsyncMock())


def _row_with_token(token: str) -> dict:
    """A row carrying just the fencing token the stand-down reads back.

    Deliberately has no ``status``: dispatch's Branch A only engages on
    provisioning/error rows, so this feeds the stand-down's token read without
    disturbing tests that exercise Branch B.
    """
    return {"provision_attempt_token": token}


def _stub_row(monkeypatch, token: str) -> None:
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_row_with_token(token)),
    )


async def _await_inline(owner_id: str):
    """Await the spawned inline attempt task for this owner (if any)."""
    entry = provision_dispatch._INLINE_ATTEMPTS.get(owner_id)
    if entry:
        await entry.task


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
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    assert claim_calls["name"] == provision_dispatch.execution_name("org_abc", claim_calls["claim"])
    # Claim, THEN start the watchdog, THEN run attempt 0 in-process.
    assert order == ["claimed", "started", "inline"]
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == provision_dispatch.execution_name("org_abc", claim_calls["claim"])
    assert kwargs["input"] == provision_dispatch.execution_input("org_abc", claim_calls["claim"])
    # The registry entry was popped by the attempt's own finally.
    assert "org_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_branch_b_start_failure_still_runs_attempt_unwatched(monkeypatch, sfn):
    # SFN unreachable. Refusing to spawn would leave a LEASED row with nothing
    # running at all — worse than the unwatched attempt, which makes progress
    # and leaves the row honest however it ends. Deliberate degrade to the
    # pre-watchdog behaviour; the next poll re-attaches a watchdog.
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
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    runner.assert_awaited_once_with("org_abc", claim_calls["claim"])
    # The claim is NOT released out from under the attempt that is using it.
    release.assert_not_awaited()


async def test_branch_b_closed_execution_still_runs_attempt_unwatched(monkeypatch, sfn):
    # An 8-hex name collision on a FRESH claim: the name embeds claim_id[:8]
    # and a closed execution already holds it, so this claim can never be
    # watched. Same rule — run it; on failure the runner leaves the row at
    # error and Branch A falls through to a fresh claim + name.
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
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    runner.assert_awaited_once_with("org_abc", claim_calls["claim"])
    release.assert_not_awaited()


async def test_branch_b_never_leaves_a_claim_with_nothing_running(monkeypatch, sfn):
    # The invariant behind both cases above, asserted directly: whatever
    # StartExecution does, a won claim always ends up with a live attempt.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    for outcome in (None, _already_exists_error(), RuntimeError("sfn exploded")):
        provision_dispatch._INLINE_ATTEMPTS.clear()
        runner = AsyncMock(return_value=0)
        monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
        sfn.start_execution.side_effect = outcome
        await provision_dispatch.dispatch_provision("org_abc")
        await _await_inline("org_abc")
        runner.assert_awaited_once()


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
    accepted = await provision_dispatch.dispatch_provision("org_abc")
    assert accepted is False
    sfn.start_execution.assert_not_called()


async def test_stamped_running_row_reissues_same_start(monkeypatch, sfn):
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    await provision_dispatch.dispatch_provision("org_abc")
    claim.assert_not_awaited()  # branch A never mints a new claim
    assert sfn.start_execution.call_args.kwargs["name"] == name


async def test_stamped_error_row_reissues_without_new_claim(monkeypatch, sfn):
    # During in-machine retries the row honestly reads error/error_retrying;
    # dispatch MUST still land on branch A (idempotent re-issue), or every ~3s
    # poll mints a second concurrent execution — the duplicate-mint incident.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
    row = {
        "status": "error",
        "substatus": "error_retrying",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    await provision_dispatch.dispatch_provision("org_abc")
    claim.assert_not_awaited()  # branch A never mints a new claim
    assert sfn.start_execution.call_args.kwargs["name"] == name


async def test_stamped_closed_execution_falls_through_to_fresh_inline_claim(monkeypatch, sfn):
    # Branch A "closed" is the design's "execution CLOSED → mint fresh claim"
    # leg: without the fall-through, a provisioning row whose execution closed
    # without a terminal row write is wedged forever.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
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
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    # Branch B ran: a fresh claim was minted, watched, and attempt 0 ran inline.
    assert claim_calls["claim"] != "claimuuid-1234"
    runner.assert_awaited_once_with("org_abc", claim_calls["claim"])
    assert sfn.start_execution.call_count == 2
    assert sfn.start_execution.call_args.kwargs["name"] == provision_dispatch.execution_name(
        "org_abc", claim_calls["claim"]
    )


async def test_branch_a_transient_failure_leaves_row_untouched(monkeypatch, sfn):
    # A transient StartExecution failure on branch A must NOT release the row
    # to error — the claim is not this dispatcher's to release.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
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
    await provision_dispatch.dispatch_provision("org_abc")
    claim.assert_not_awaited()
    release.assert_not_awaited()


async def test_branch_a_retries_the_start_for_an_UNWATCHED_live_attempt(monkeypatch, sfn):
    # Branch B degraded (StartExecution failed) but still spawned. If Branch A
    # then stood down for the live attempt the way it does for a watched one,
    # the attempt would suppress its own recovery for its whole 30-minute
    # life, and a hard kill in that window would leave the claim with nothing.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    gate = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        await gate.wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234", watched=False)
    await asyncio.sleep(0)

    # Poll 1: SFN still down — retried, still unwatched.
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "StartExecution"
    )
    await provision_dispatch.dispatch_provision("org_abc")
    assert sfn.start_execution.call_count == 1
    assert provision_dispatch._INLINE_ATTEMPTS["org_abc"].watched is False

    # Poll 2: SFN recovered — the watchdog attaches and the flag flips.
    sfn.start_execution.side_effect = None
    await provision_dispatch.dispatch_provision("org_abc")
    assert sfn.start_execution.call_count == 2
    assert provision_dispatch._INLINE_ATTEMPTS["org_abc"].watched is True

    # Poll 3: now watched — no further redundant API calls.
    await provision_dispatch.dispatch_provision("org_abc")
    assert sfn.start_execution.call_count == 2
    gate.set()
    await _await_inline("org_abc")


async def test_branch_a_never_mints_a_second_claim_while_an_attempt_is_live(monkeypatch, sfn):
    # A live UNWATCHED attempt whose name is held by a CLOSED execution must
    # not fall through to Branch B: that would mint a second claim and run a
    # second runner for the same owner.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
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
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234", watched=False)
    await asyncio.sleep(0)
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("org_abc")
    claim.assert_not_awaited()  # no second claim
    assert provision_dispatch._INLINE_ATTEMPTS["org_abc"].claim_id == "claimuuid-1234"
    gate.set()
    await _await_inline("org_abc")


async def test_branch_a_stands_down_while_inline_attempt_live(monkeypatch, sfn):
    # The ~3s poll cadence re-enters dispatch while attempt 0 runs in this
    # process; Branch A must NOT hand the live claim to the state machine.
    name = provision_dispatch.execution_name("org_abc", "claimuuid-1234")
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
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await provision_dispatch.dispatch_provision("org_abc")
    sfn.start_execution.assert_not_called()
    claim.assert_not_awaited()
    gate.set()
    await _await_inline("org_abc")


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
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    release.assert_not_awaited()
    assert sfn.start_execution.call_count == 1  # the claim-time watchdog only
    assert "org_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_inline_crash_stands_the_row_down(monkeypatch, sfn):
    # A crash anywhere in the attempt (including the pre-spawn stamp) goes
    # through the stand-down, which fences on the token READ BACK from the row
    # rather than assuming the stamp landed.
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(claim=claim_id)
        # From here on the row reads back the inline token — the stamp landed.
        _stub_row(monkeypatch, provision_dispatch.inline_attempt_token(claim_id))
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", AsyncMock(side_effect=RuntimeError("boom")))
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("org_abc", claim_calls["claim"])
    assert kwargs["last_error"] == "inline provision attempt crashed"
    assert kwargs["retryable"] is True
    # After an SFN takeover re-stamps the token, the read sees a value that is
    # neither of ours and the stand-down writes nothing at all.
    assert kwargs["attempt_token"] == provision_dispatch.inline_attempt_token(claim_calls["claim"])


async def test_stand_down_writes_nothing_when_the_machine_owns_the_row(monkeypatch, sfn):
    # An SFN retry re-stamped a fresh UUID: the row belongs to the machine's
    # attempt now, so the stand-down must not flip it to error mid-run.
    _stub_row(monkeypatch, "some-fresh-sfn-uuid")
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    await provision_dispatch._stand_down_attempt("org_abc", "claimuuid-1234", "cause")
    release.assert_not_awaited()


async def test_stand_down_writes_nothing_when_the_row_read_fails(monkeypatch, sfn):
    # Never write blind: without knowing the token we cannot fence, and the
    # watchdog's backstop still covers the row.
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "get_by_owner_id",
        AsyncMock(side_effect=RuntimeError("ddb down")),
    )
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    await provision_dispatch._stand_down_attempt("org_abc", "claimuuid-1234", "cause")  # must not raise
    release.assert_not_awaited()


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
    code = await provision_dispatch._runner_subprocess("org_abc", "claim-1")
    assert code == 0
    assert captured["argv"] == (
        sys.executable,
        "-m",
        "scripts.provision_runner",
        "--owner",
        "org_abc",
        "--claim",
        "claim-1",
    )
    assert captured["env"]["PROVISION_ATTEMPT_TOKEN"] == provision_dispatch.inline_attempt_token("claim-1")


async def test_handoff_terminates_child_then_stands_the_row_down(monkeypatch, sfn):
    # Shutdown: cancel + await the attempt (the cancel path terminates the
    # child), then assure the watchdog and make the row honest. Without the
    # stand-down a deploy roll costs the owner the 40-tick backstop (~40 min)
    # instead of one tick — and, if Branch B had degraded to unwatched, the
    # claim would have neither an execution nor a runner.
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
    sfn.start_execution.side_effect = lambda **kw: order.append("started")
    _stub_row(monkeypatch, provision_dispatch.inline_attempt_token("claimuuid-1234"))
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await asyncio.sleep(0)  # let the attempt reach its await
    await provision_dispatch.handoff_inflight_provisions()
    # Child dies FIRST — no straggler may outlive this instance — then the
    # watchdog is assured.
    assert order == ["cancelled", "started"]
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == provision_dispatch.execution_name("org_abc", "claimuuid-1234")
    assert kwargs["input"] == provision_dispatch.execution_input("org_abc", "claimuuid-1234")
    release.assert_awaited_once()
    assert release.await_args.kwargs["attempt_token"] == provision_dispatch.inline_attempt_token("claimuuid-1234")
    assert release.await_args.kwargs["retryable"] is True
    assert "org_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_handoff_of_a_QUEUED_attempt_releases_on_the_claim_time_token(monkeypatch, sfn):
    # An attempt cancelled while still waiting for a slot never stamped the
    # inline token, so the row still carries the CLAIM-TIME token. Fencing the
    # release on the inline token here would be conditionally rejected — a
    # silent no-op leaving the row at "provisioning" to wait out the 40-tick
    # backstop.
    monkeypatch.setattr(provision_dispatch, "_INLINE_CONCURRENCY", asyncio.Semaphore(0))
    stamp = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "claim_attempt", stamp)
    # Never stamped, so the row still reads back the claim-time token.
    _stub_row(monkeypatch, "claimuuid-1234")
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await asyncio.sleep(0)  # let the attempt reach the semaphore wait
    await provision_dispatch.handoff_inflight_provisions()
    stamp.assert_not_awaited()  # never got a slot, so never stamped
    runner.assert_not_awaited()
    release.assert_awaited_once()
    assert release.await_args.kwargs["attempt_token"] == "claimuuid-1234"


async def test_handoff_of_a_RUNNING_attempt_releases_on_the_inline_token(monkeypatch, sfn):
    # The other half of the same rule: once the attempt holds a slot it has
    # stamped the inline token, so THAT is what the release must fence on —
    # the claim-time token would also match a live SFN attempt-0 runner and
    # could clobber machine-owned state.
    gate = asyncio.Event()
    running = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        # Reaching here proves the slot was acquired and the token stamped —
        # deterministic, rather than counting event-loop turns.
        running.set()
        await gate.wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    _stub_row(monkeypatch, provision_dispatch.inline_attempt_token("claimuuid-1234"))
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await running.wait()
    await provision_dispatch.handoff_inflight_provisions()
    release.assert_awaited_once()
    assert release.await_args.kwargs["attempt_token"] == provision_dispatch.inline_attempt_token("claimuuid-1234")


async def test_claim_taken_over_while_queued_does_not_run_or_write(monkeypatch, sfn):
    # The stamp is conditioned on the claim, so a takeover during the queue
    # wait surfaces as ProvisionSuperseded. The attempt must not run and must
    # not touch the row — the successor owns it now.
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "claim_attempt",
        AsyncMock(side_effect=provision_dispatch.container_repo.ProvisionSuperseded("taken over")),
    )
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", runner)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await _await_inline("org_abc")
    runner.assert_not_awaited()
    release.assert_not_awaited()
    # The permit must still come back or the next attempt queues forever.
    assert provision_dispatch._INLINE_CONCURRENCY._value == 4


async def test_handoff_survives_a_failed_release(monkeypatch, sfn):
    # The release is best-effort: the watchdog's backstop still covers the row,
    # so a DDB failure here must not take down the shutdown path.
    gate = asyncio.Event()

    async def blocked_runner(owner_id, claim_id):
        await gate.wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", blocked_runner)
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "release_provision_to_error",
        AsyncMock(side_effect=RuntimeError("ddb down")),
    )
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await asyncio.sleep(0)
    await provision_dispatch.handoff_inflight_provisions()  # must not raise
    sfn.start_execution.assert_called_once()


async def test_inline_timeout_terminates_then_stands_the_row_down(monkeypatch, sfn):
    # The machine bounds every ECS attempt at 30 min (taskTimeout); attempt 0
    # gets the same ceiling. On expiry the child is terminated, then the row is
    # stood down so a watchdog acts on the NEXT tick rather than at the
    # backstop — and so an unwatched attempt (degraded Branch B) does not
    # leave the claim with nothing at all.
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
    _stub_row(monkeypatch, provision_dispatch.inline_attempt_token("claimuuid-1234"))
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await _await_inline("org_abc")
    assert cancelled.is_set()
    sfn.start_execution.assert_called_once()
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("org_abc", "claimuuid-1234")
    assert kwargs["last_error"] == "inline provision attempt timed out"
    assert kwargs["retryable"] is True
    # Fenced on the token read back from the row, so a machine takeover (whose
    # runner re-stamped a fresh UUID) makes this a no-op instead of clobbering
    # machine-owned state.
    assert kwargs["attempt_token"] == provision_dispatch.inline_attempt_token("claimuuid-1234")
    assert "org_abc" not in provision_dispatch._INLINE_ATTEMPTS


async def test_unwatched_attempt_that_times_out_still_gets_a_watchdog(monkeypatch, sfn):
    # The composition bug: Branch B degrades to unwatched (StartExecution
    # fails), then the attempt times out. Previously that terminated the only
    # runner and returned, leaving a leased row with neither an execution nor
    # a worker. The stand-down retries the start — SFN may well have recovered
    # during the 30 minutes — and makes the row honest either way.
    monkeypatch.setattr(provision_dispatch, "_INLINE_ATTEMPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        _stub_row(monkeypatch, provision_dispatch.inline_attempt_token(claim_id))
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)

    async def wedged_runner(owner_id, claim_id):
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_subprocess", wedged_runner)
    # First call (Branch B) fails; the stand-down's retry succeeds.
    sfn.start_execution.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "StartExecution"),
        None,
    ]
    await provision_dispatch.dispatch_provision("org_abc")
    await _await_inline("org_abc")
    assert sfn.start_execution.call_count == 2  # degraded start, then recovered
    release.assert_awaited_once()


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
    provision_dispatch._spawn_inline("org_abc", "claimuuid-1234")
    await _await_inline("org_abc")
    runner.assert_not_awaited()  # never got a slot
    release.assert_awaited_once()
    args, kwargs = release.await_args
    assert args == ("org_abc", "claimuuid-1234")
    assert kwargs["last_error"] == "inline queue saturated — machine takes over"
    assert kwargs["retryable"] is True
    # Fenced on the claim-time token (try_claim_provision stamps claim_id):
    # a retry that already re-stamped it owns the row, so this no-ops.
    assert kwargs["attempt_token"] == "claimuuid-1234"
    assert "org_abc" not in provision_dispatch._INLINE_ATTEMPTS


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


async def test_non_org_owner_is_refused_before_any_claim(monkeypatch, sfn):
    """Personal workspaces no longer exist: the single provisioning funnel refuses
    a user_ owner before claiming a containers row or starting an SFN execution."""
    claim = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    await provision_dispatch.dispatch_provision("user_abc")
    claim.assert_not_awaited()
    sfn.start_execution.assert_not_called()
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS
