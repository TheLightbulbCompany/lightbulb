"""Tests for core/services/provision_dispatch.py (SFN dispatch path)."""

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


async def test_fresh_dispatch_claims_stamps_and_starts(monkeypatch, sfn):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))
    claim_calls = {}

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        claim_calls.update(owner=owner_id, claim=claim_id, name=execution_name)
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    await provision_dispatch.dispatch_provision("user_abc")
    assert claim_calls["name"] == provision_dispatch.execution_name("user_abc", claim_calls["claim"])
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == claim_calls["name"]
    payload = json.loads(kwargs["input"])
    assert payload["owner_id"] == "user_abc" and payload["attempt"] == 0
    # `uv run` is load-bearing: the backend image's deps live in a uv-managed
    # venv with no ENTRYPOINT/PATH setup, so a bare `python -m` cannot import
    # the app.
    assert payload["command"][:5] == ["uv", "run", "python", "-m", "scripts.provision_runner"]


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


async def test_stamped_closed_execution_falls_through_to_fresh_claim(monkeypatch, sfn):
    # Branch A "closed" is the design's "execution CLOSED → mint fresh claim"
    # leg: without the fall-through, a provisioning row whose execution closed
    # without a terminal row write is wedged forever (branch A no-ops on every
    # poll and the claim conditions refuse a fresh lease).
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
    sfn.start_execution.side_effect = [_already_exists_error(), MagicMock()]
    await provision_dispatch.dispatch_provision("user_abc")
    # Branch B ran: a fresh claim was minted and started under a NEW name.
    assert claim_calls["claim"] != "claimuuid-1234"
    assert sfn.start_execution.call_count == 2
    second = sfn.start_execution.call_args_list[1].kwargs
    assert second["name"] == claim_calls["name"] != name


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


async def test_fresh_claim_name_collision_releases_to_error(monkeypatch, sfn):
    # ExecutionAlreadyExists on a FRESH claim = 8-hex name collision with a
    # closed execution; the claim can never run under that name, so release it
    # to error and let the next poll mint a new claim/name.
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    sfn.start_execution.side_effect = _already_exists_error()
    await provision_dispatch.dispatch_provision("user_abc")
    release.assert_awaited_once()


async def test_start_failure_releases_claim_to_error(monkeypatch, sfn):
    monkeypatch.setattr(provision_dispatch.container_repo, "get_by_owner_id", AsyncMock(return_value=None))

    async def fake_claim(owner_id, *, claim_id=None, execution_name=None):
        return claim_id

    monkeypatch.setattr(provision_dispatch.container_repo, "try_claim_provision", fake_claim)
    release = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", release)
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )
    await provision_dispatch.dispatch_provision("user_abc")
    release.assert_awaited_once()


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
