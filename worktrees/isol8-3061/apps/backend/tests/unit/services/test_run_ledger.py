from unittest.mock import AsyncMock, patch

import pytest

from core.services import run_ledger

ROOM = "22222222-2222-4222-8222-222222222222"


def _row(status, deadline):
    return {
        "runId": "r1",
        "ownerId": "o",
        "roomId": ROOM,
        "agentId": "a1",
        "status": status,
        "deadlineAt": deadline,
        "eventKind": "user_request",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,error_kind", [("dispatched", "agent_did_not_start"), ("started", "agent_stopped_responding")]
)
async def test_overdue_runs_close_as_error_with_an_error_and_run_ended_frame(monkeypatch, status, error_kind):
    monkeypatch.setattr(run_ledger, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(run_ledger.time, "time", lambda: 1_000)
    ended_row = {**_row("ended", 900), "outcome": "error", "errorKind": error_kind}
    with (
        patch.object(
            run_ledger.messaging_repo, "list_overdue_runs", AsyncMock(return_value=[{"ownerId": "o", "runId": "r1"}])
        ),
        patch.object(run_ledger.messaging_repo, "get_run", AsyncMock(return_value=_row(status, 900))),
        patch.object(run_ledger.messaging_repo, "end_run", AsyncMock(return_value=ended_row)) as end,
        patch.object(run_ledger, "publish_run_error_frames", AsyncMock()) as frames,
        patch.object(run_ledger, "post_run_failure_message", AsyncMock()) as note,
    ):
        assert await run_ledger.sweep_runs_once() == 1
    end.assert_awaited_once_with("o", "r1", outcome="error", error_kind=error_kind)
    frames.assert_awaited_once_with(ended_row, error_kind)
    note.assert_awaited_once_with("o", ended_row, "error", error_kind)


@pytest.mark.asyncio
async def test_rows_that_closed_or_extended_meanwhile_are_skipped(monkeypatch):
    monkeypatch.setattr(run_ledger, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(run_ledger.time, "time", lambda: 1_000)
    with (
        patch.object(
            run_ledger.messaging_repo,
            "list_overdue_runs",
            AsyncMock(return_value=[{"ownerId": "o", "runId": "r1"}, {"ownerId": "o", "runId": "r2"}]),
        ),
        patch.object(
            run_ledger.messaging_repo, "get_run", AsyncMock(side_effect=[_row("ended", 900), _row("started", 2_000)])
        ),
        patch.object(run_ledger.messaging_repo, "end_run", AsyncMock()) as end,
    ):
        assert await run_ledger.sweep_runs_once() == 0
    end.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_poisoned_candidate_does_not_stop_the_tick(monkeypatch):
    monkeypatch.setattr(run_ledger, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(run_ledger.time, "time", lambda: 1_000)
    ended_row = {**_row("ended", 900), "outcome": "error", "errorKind": "agent_did_not_start"}
    with (
        patch.object(
            run_ledger.messaging_repo,
            "list_overdue_runs",
            AsyncMock(return_value=[{"ownerId": "o", "runId": "r1"}, {"ownerId": "o", "runId": "r2"}]),
        ),
        patch.object(
            run_ledger.messaging_repo,
            "get_run",
            AsyncMock(side_effect=[RuntimeError("boom"), {**_row("dispatched", 900), "runId": "r2"}]),
        ),
        patch.object(run_ledger.messaging_repo, "end_run", AsyncMock(return_value=ended_row)) as end,
        patch.object(run_ledger, "publish_run_error_frames", AsyncMock()),
        patch.object(run_ledger, "post_run_failure_message", AsyncMock()),
    ):
        assert await run_ledger.sweep_runs_once() == 1
    end.assert_awaited_once_with("o", "r2", outcome="error", error_kind="agent_did_not_start")
