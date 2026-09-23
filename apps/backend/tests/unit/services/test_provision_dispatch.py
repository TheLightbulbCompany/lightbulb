"""Tests for core/services/provision_dispatch.py (stateless SFN submitter).

A RUNNING execution named ``provision-{owner}-…`` owns the owner's
provision; anything else is re-driveable. No claim, no lease, no row fence.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import provision_dispatch

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:lightbulb-dev-provision"
# A real-shaped Clerk org id (org_ + 27 chars).
ORG = "org_3JL4abcdefghijklmnopqrstu"


@pytest.fixture
def sfn(monkeypatch):
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"executions": []}]
    monkeypatch.setattr(provision_dispatch, "_client", lambda: client)
    monkeypatch.setattr(provision_dispatch.settings, "PROVISION_STATE_MACHINE_ARN", ARN)
    return client


@pytest.fixture
def mark(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "mark_provisioning", mock)
    return mock


def _running(sfn, *pages):
    sfn.get_paginator.return_value.paginate.return_value = [{"executions": list(p)} for p in pages]


async def test_no_running_execution_starts_one_and_marks_the_row(sfn, mark):
    assert await provision_dispatch.dispatch_provision(ORG) is True

    sfn.get_paginator.assert_called_once_with("list_executions")
    sfn.get_paginator.return_value.paginate.assert_called_once_with(stateMachineArn=ARN, statusFilter="RUNNING")
    mark.assert_awaited_once_with(ORG)
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == ARN
    assert kwargs["name"].startswith(f"provision-{ORG}-")
    assert len(kwargs["name"].rsplit("-", 1)[1]) == 8
    assert len(kwargs["name"]) <= 80  # SFN's execution-name limit
    assert json.loads(kwargs["input"])["owner_id"] == ORG


async def test_running_execution_for_owner_is_a_noop(sfn, mark):
    # Found on the SECOND page: the check must paginate.
    _running(
        sfn,
        [{"name": "provision-org_other-1234abcd", "status": "RUNNING"}],
        [{"name": f"provision-{ORG}-deadbeef", "status": "RUNNING"}],
    )
    assert await provision_dispatch.dispatch_provision(ORG) is True
    sfn.start_execution.assert_not_called()
    mark.assert_not_awaited()


async def test_another_owners_running_execution_does_not_block(sfn, mark):
    # A prefix of ORG's id is a different owner; the filter includes the trailing dash.
    _running(sfn, [{"name": f"provision-{ORG[:-1]}-deadbeef", "status": "RUNNING"}])
    assert await provision_dispatch.dispatch_provision(ORG) is True
    assert sfn.start_execution.call_count == 1


async def test_each_start_gets_a_fresh_name(sfn, mark):
    await provision_dispatch.dispatch_provision(ORG)
    await provision_dispatch.dispatch_provision(ORG)
    first, second = (c.kwargs["name"] for c in sfn.start_execution.call_args_list)
    assert first != second


async def test_start_failure_returns_false_and_releases_the_row_to_a_retryable_error(sfn, mark, monkeypatch):
    """Nothing re-drives a lost dispatch any more, so the row must not sit on "provisioning"
    with no execution: it becomes an error the stepper offers Retry on."""
    error = AsyncMock()
    monkeypatch.setattr(provision_dispatch.container_repo, "mark_provision_error", error)
    sfn.start_execution.side_effect = Exception("sfn down")
    assert await provision_dispatch.dispatch_provision(ORG) is False
    error.assert_awaited_once_with(ORG, last_error="provision could not start: Exception", retryable=True)


async def test_a_failed_error_write_still_never_raises(sfn, mark, monkeypatch):
    monkeypatch.setattr(
        provision_dispatch.container_repo, "mark_provision_error", AsyncMock(side_effect=RuntimeError("ddb"))
    )
    sfn.start_execution.side_effect = Exception("sfn down")
    assert await provision_dispatch.dispatch_provision(ORG) is False


async def test_list_failure_starts_nothing(sfn, mark, monkeypatch):
    monkeypatch.setattr(provision_dispatch.container_repo, "mark_provision_error", AsyncMock())
    sfn.get_paginator.return_value.paginate.side_effect = Exception("throttled")
    assert await provision_dispatch.dispatch_provision(ORG) is False
    sfn.start_execution.assert_not_called()


async def test_non_org_owner_is_refused_before_any_aws_call(sfn, mark):
    assert await provision_dispatch.dispatch_provision("user_notanorg") is False
    sfn.get_paginator.assert_not_called()
    sfn.start_execution.assert_not_called()
    mark.assert_not_awaited()


async def test_running_execution_returns_the_owners_summary(sfn):
    summary = {"name": f"provision-{ORG}-deadbeef", "status": "RUNNING"}
    _running(sfn, [summary])
    assert await provision_dispatch.running_execution(ORG) == summary
    assert await provision_dispatch.running_execution("org_nobody") is None


def test_execution_input_is_the_owner_and_the_provision_mode():
    assert json.loads(provision_dispatch.execution_input(ORG)) == {"owner_id": ORG, "mode": "provision"}


async def test_reconcile_starts_a_reconcile_execution_without_checks_or_row_writes(sfn, mark):
    assert await provision_dispatch.dispatch_reconcile(ORG) is True
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == ARN
    assert kwargs["name"].startswith(f"reconcile-{ORG}-")
    assert len(kwargs["name"]) <= 80
    assert json.loads(kwargs["input"]) == {"owner_id": ORG, "mode": "reconcile"}
    sfn.get_paginator.assert_not_called()
    mark.assert_not_awaited()


async def test_reconcile_refuses_a_non_org_owner(sfn, mark, monkeypatch):
    metrics = []
    monkeypatch.setattr(provision_dispatch, "emit_provision_metric", metrics.append)
    assert await provision_dispatch.dispatch_reconcile("user_notanorg") is False
    sfn.start_execution.assert_not_called()
    assert metrics == ["refused_non_org"]


async def test_reconcile_start_failure_returns_false(sfn, mark, monkeypatch):
    metrics = []
    monkeypatch.setattr(provision_dispatch, "emit_provision_metric", metrics.append)
    sfn.start_execution.side_effect = Exception("sfn down")
    assert await provision_dispatch.dispatch_reconcile(ORG) is False
    assert metrics == ["dispatch_start_failed"]
