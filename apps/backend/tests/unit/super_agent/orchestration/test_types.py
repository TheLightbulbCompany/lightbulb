"""Temporal payloads carry ids, enums and numbers only: no message text crosses Temporal (wave-2 O Task 1)."""

import dataclasses
from typing import get_args

import pytest

from core import tasks_schema as ts
from core.temporal import types as tt

ALLOWED = {
    "ws",
    "trigger_id",
    "lane",
    "admission",
    "task_id",
    "reason",
    "kind",
    "by_user_id",
    "status",
    "expected_version",
    "run_id",
    "usage",
    "batch_id",
    "trigger_ids",
    "started_task_ids",
    "posted_message_ids",
    "fallback",
    "n",
    "outcome",
    "error",
    "queue",
    "seen",  # RouterState: refs and trigger ids
    # RunUsage: numbers only
    "input_tokens",
    "output_tokens",
    "cost_usd",
}


def _payloads():
    return [v for v in vars(tt).values() if dataclasses.is_dataclass(v) and v.__module__ == tt.__name__]


def test_payload_types_carry_no_text():
    payloads = _payloads()
    assert {p.__name__ for p in payloads} >= {
        "TriggerRef",
        "TaskStart",
        "TaskInputRef",
        "StopRequest",
        "StopResult",
        "ActionRequest",
        "RunEvent",
        "RouterTurnIn",
        "RouterTurnOut",
        "AttemptRef",
        "WorkerTurnIn",
        "WorkerTurnOut",
    }
    for p in payloads:
        assert p.__dataclass_params__.frozen, p
        assert {f.name for f in dataclasses.fields(p)} <= ALLOWED, p


def test_error_is_a_class_name_only():
    assert tt.WorkerTurnOut("failed", "RunLost").error == "RunLost"
    with pytest.raises(ValueError):
        tt.WorkerTurnOut("failed", "the model said something private")


def test_task_start_reasons():
    assert set(get_args(tt.TaskStartReason)) == {
        "router",
        "propose_start",
        "reopen",
        "retry",
        "schedule",
        "migrated",
        "reconcile",
    }


def test_enums_mirror_tasks_schema():
    assert get_args(tt.TaskInputKind) == get_args(ts.TaskInputKind)
    assert get_args(tt.AttemptOutcome) == get_args(ts.AttemptOutcome)
    assert get_args(tt.Admission) == get_args(ts.Admission)
