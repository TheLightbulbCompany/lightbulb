"""Tests for core/services/provision_dispatch.py (SFN-only funnel).

The backend submits (StartExecution) and forgets; attempt work runs only on
the machine. These tests pin: claim-then-start ordering, the bool return
contract (callers with recovery markers retain them on False), Branch A
re-issue without a second claim, and start-failure release to error.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


def _stub_claim(monkeypatch, won: str | None):
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "try_claim_provision",
        AsyncMock(return_value=won),
    )


def _stub_row(monkeypatch, row):
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=row),
    )


def _stub_release(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "release_provision_to_error", mock)
    return mock


async def test_fresh_dispatch_claims_then_starts_and_returns_true(monkeypatch, sfn):
    _stub_row(monkeypatch, None)
    _stub_claim(monkeypatch, "claim-1")
    assert await provision_dispatch.dispatch_provision("org_x") is True
    provision_dispatch.container_repo.try_claim_provision.assert_awaited_once()
    assert sfn.start_execution.call_count == 1


async def test_claim_lost_starts_nothing_and_returns_false(monkeypatch, sfn):
    _stub_row(monkeypatch, None)
    _stub_claim(monkeypatch, None)
    assert await provision_dispatch.dispatch_provision("org_x") is False
    sfn.start_execution.assert_not_called()


async def test_branch_b_start_failure_releases_claim_to_error(monkeypatch, sfn):
    _stub_row(monkeypatch, None)
    _stub_claim(monkeypatch, "claim-9")
    sfn.start_execution.side_effect = Exception("sfn down")
    release = _stub_release(monkeypatch)
    assert await provision_dispatch.dispatch_provision("org_x") is False
    # Released on the freshly minted claim (whatever uuid Branch B minted —
    # the mock's fixed return is not what dispatch uses).
    minted = provision_dispatch.container_repo.try_claim_provision.await_args.kwargs["claim_id"]
    release.assert_awaited_once_with(
        "org_x",
        minted,
        last_error="provision dispatch could not start watchdog (outcome=failed)",
        retryable=True,
        attempt_token=minted,
    )


async def test_branch_b_closed_name_releases_claim_to_error(monkeypatch, sfn):
    _stub_row(monkeypatch, None)
    _stub_claim(monkeypatch, "claim-9")
    sfn.start_execution.side_effect = _already_exists_error()
    release = _stub_release(monkeypatch)
    assert await provision_dispatch.dispatch_provision("org_x") is False
    release.assert_awaited_once()


async def test_stamped_provisioning_row_reissues_same_start(monkeypatch, sfn):
    claim = "claim-abc"
    _stub_row(
        monkeypatch,
        {
            "status": "provisioning",
            "provision_claim_id": claim,
            "provision_execution_name": provision_dispatch.execution_name("org_x", claim),
        },
    )
    assert await provision_dispatch.dispatch_provision("org_x") is True
    assert sfn.start_execution.call_count == 1
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == provision_dispatch.execution_name("org_x", claim)


async def test_stamped_error_row_reissues_without_new_claim(monkeypatch, sfn):
    claim = "claim-abc"
    _stub_row(
        monkeypatch,
        {
            "status": "error",
            "provision_claim_id": claim,
            "provision_execution_name": provision_dispatch.execution_name("org_x", claim),
        },
    )
    with patch.object(provision_dispatch.container_repo, "try_claim_provision", new=AsyncMock()) as mock_claim:
        assert await provision_dispatch.dispatch_provision("org_x") is True
    mock_claim.assert_not_awaited()
    assert sfn.start_execution.call_count == 1


async def test_branch_a_transient_failure_leaves_row_untouched(monkeypatch, sfn):
    claim = "claim-abc"
    _stub_row(
        monkeypatch,
        {
            "status": "provisioning",
            "provision_claim_id": claim,
            "provision_execution_name": provision_dispatch.execution_name("org_x", claim),
        },
    )
    sfn.start_execution.side_effect = Exception("sfn down")
    release = _stub_release(monkeypatch)
    assert await provision_dispatch.dispatch_provision("org_x") is False
    # The claim is not ours to release — a later poll retries.
    release.assert_not_awaited()


async def test_branch_a_closed_execution_falls_through_to_fresh_claim(monkeypatch, sfn):
    claim = "claim-abc"
    _stub_row(
        monkeypatch,
        {
            "status": "provisioning",
            "provision_claim_id": claim,
            "provision_execution_name": provision_dispatch.execution_name("org_x", claim),
        },
    )
    _stub_claim(monkeypatch, "claim-fresh")
    # First start (re-issue) reports closed; the fresh claim's start must
    # succeed for the dispatch to own the work.
    sfn.start_execution.side_effect = [_already_exists_error(), MagicMock()]
    assert await provision_dispatch.dispatch_provision("org_x") is True
    assert sfn.start_execution.call_count == 2
    provision_dispatch.container_repo.try_claim_provision.assert_awaited_once()


async def test_branch_a_mismatched_stamp_takes_fresh_claim_path(monkeypatch, sfn):
    _stub_row(
        monkeypatch,
        {
            "status": "provisioning",
            "provision_claim_id": "claim-old",
            "provision_execution_name": "provision-org_x-deadbeef",
        },
    )
    _stub_claim(monkeypatch, "claim-new")
    assert await provision_dispatch.dispatch_provision("org_x") is True
    provision_dispatch.container_repo.try_claim_provision.assert_awaited_once()
    assert sfn.start_execution.call_count == 1


async def test_non_org_owner_is_refused_before_any_claim(monkeypatch, sfn):
    with patch.object(provision_dispatch.container_repo, "try_claim_provision", new=AsyncMock()) as mock_claim:
        assert await provision_dispatch.dispatch_provision("user_notanorg") is False
    mock_claim.assert_not_awaited()
    sfn.start_execution.assert_not_called()


async def test_dispatch_never_raises_returning_false(monkeypatch, sfn):
    monkeypatch.setattr(
        provision_dispatch.container_repo,
        "get_by_owner_id",
        AsyncMock(side_effect=Exception("ddb down")),
    )
    assert await provision_dispatch.dispatch_provision("org_x") is False


async def test_execution_input_is_byte_identical_across_calls():
    first = provision_dispatch.execution_input("org_x", "claim-1")
    second = provision_dispatch.execution_input("org_x", "claim-1")
    assert first == second
    assert json.loads(first)["attempt_token"] == "claim-1"


async def test_execution_input_command_shape():
    body = json.loads(provision_dispatch.execution_input("org_x", "claim-1"))
    assert body["command"] == [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "scripts.provision_runner",
        "--owner",
        "org_x",
        "--claim",
        "claim-1",
    ]
    assert body["owner_id"] == "org_x"
