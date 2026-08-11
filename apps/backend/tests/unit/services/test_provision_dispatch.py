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


async def test_fresh_dispatch_claims_and_runs_inline(monkeypatch, sfn):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(owner=owner_id, claim=claim_id, name=execution_name)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(provision_dispatch, "_runner_run", runner)
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    assert claim_calls["name"] == provision_dispatch.execution_name("user_abc", claim_calls["claim"])
    # Attempt 0 ran IN-PROCESS under the distinct inline token; the state
    # machine was never started.
    runner.assert_awaited_once_with(
        "user_abc",
        claim_calls["claim"],
        provision_dispatch.inline_attempt_token(claim_calls["claim"]),
    )
    sfn.start_execution.assert_not_called()
    # The registry entry was popped by the attempt's own finally.
    assert "user_abc" not in provision_dispatch._INLINE_ATTEMPTS


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
    # without a terminal row write is wedged forever. (SWEEP -- not built in
    # this PR -- is the design's real fix for this; see
    # describe_dispatch_execution's # SWEEP: comment.)
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
    monkeypatch.setattr(provision_dispatch, "_runner_run", runner)
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    # Branch B ran: a fresh claim was minted and attempt 0 ran inline.
    assert claim_calls["claim"] != "claimuuid-1234"
    runner.assert_awaited_once_with(
        "user_abc",
        claim_calls["claim"],
        provision_dispatch.inline_attempt_token(claim_calls["claim"]),
    )
    assert sfn.start_execution.call_count == 1  # only the Branch A re-issue


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
    import threading

    name = provision_dispatch.execution_name("user_abc", "claimuuid-1234")
    row = {
        "status": "provisioning",
        "provision_claim_id": "claimuuid-1234",
        "provision_execution_name": name,
    }
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", claim)
    # threading.Event, not asyncio.Event: the runner executes on the
    # provision executor thread's own loop, so a test-loop asyncio primitive
    # cannot be awaited there.
    gate = threading.Event()

    async def blocked_runner(owner_id, claim_id, attempt_token):
        while not gate.is_set():
            await asyncio.sleep(0.005)
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_run", blocked_runner)
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await provision_dispatch.dispatch_provision("user_abc")
    sfn.start_execution.assert_not_called()
    claim.assert_not_awaited()
    gate.set()
    await _await_inline("user_abc")


async def test_inline_failure_exit_code_leaves_row_writes_to_runner(monkeypatch, sfn):
    # Exit 10/11: run() already released the row itself; the dispatcher must
    # not double-write. The stamped claim survives, so the NEXT dispatch lands
    # on Branch A and hands the claim to the state machine.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    monkeypatch.setattr(provision_dispatch, "_runner_run", AsyncMock(return_value=11))
    await provision_dispatch.dispatch_provision("user_abc")
    await _await_inline("user_abc")
    release.assert_not_awaited()
    sfn.start_execution.assert_not_called()
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
    monkeypatch.setattr(provision_dispatch, "_runner_run", AsyncMock(side_effect=RuntimeError("boom")))
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


async def test_inline_attempt_runs_off_the_server_loop(monkeypatch, sfn):
    # The runner's call tree contains sync network/EFS I/O; the attempt must
    # execute on the dedicated provision executor's thread with its own loop,
    # never on the server's.
    import threading

    seen = {}

    async def recording_runner(owner_id, claim_id, attempt_token):
        seen["thread"] = threading.current_thread().name
        seen["loop"] = asyncio.get_running_loop()
        return 0

    monkeypatch.setattr(provision_dispatch, "_runner_run", recording_runner)
    server_loop = asyncio.get_running_loop()
    provision_dispatch._spawn_inline("user_abc", "claimuuid-1234")
    await _await_inline("user_abc")
    assert seen["thread"].startswith("inline-provision")
    assert seen["loop"] is not server_loop


async def test_ddb_read_failure_is_swallowed_and_starts_nothing(monkeypatch, sfn):
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "get_by_owner_id",
        AsyncMock(
            side_effect=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetItem")
        ),
    )
    await provision_dispatch.dispatch_provision("user_abc")  # must not raise
    sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# describe_dispatch_execution — the # SWEEP: integration point (spec §7).
# SWEEP itself is NOT built in this PR; this is just the read primitive it
# will call.


async def test_describe_dispatch_execution_none_when_no_stamped_execution(monkeypatch):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    result = await provision_dispatch.describe_dispatch_execution("user_abc", "claim-1")
    assert result is None


async def test_describe_dispatch_execution_none_when_claim_mismatch(monkeypatch):
    row = {"provision_execution_name": "provision-user_abc-abcd1234", "provision_claim_id": "other-claim"}
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    result = await provision_dispatch.describe_dispatch_execution("user_abc", "claim-1")
    assert result is None


async def test_describe_dispatch_execution_calls_describe_execution(monkeypatch, sfn):
    row = {"provision_execution_name": "provision-user_abc-abcd1234", "provision_claim_id": "claim-1"}
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(
        provision_dispatch.settings,
        "PROVISION_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:isol8-dev-provision",
    )
    sfn.describe_execution.return_value = {"status": "RUNNING"}
    result = await provision_dispatch.describe_dispatch_execution("user_abc", "claim-1")
    assert result == {"status": "RUNNING"}
    kwargs = sfn.describe_execution.call_args.kwargs
    assert kwargs["executionArn"] == (
        "arn:aws:states:us-east-1:123456789012:execution:isol8-dev-provision:provision-user_abc-abcd1234"
    )
