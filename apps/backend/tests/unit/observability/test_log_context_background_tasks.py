"""Tests for correlation-id propagation into detached background tasks.

Phase 2 observability remediation: contextvars set in an HTTP middleware do
NOT propagate into ``asyncio.create_task`` / ``BackgroundTasks`` children, so
the chat / provisioning / gateway-reader tasks logged ``user_id=null``. The
``bind_log_context`` helper re-binds the available ids at the top of each task
body so the structured JSON log fields populate.

These tests assert the context helper and a retained provisioning background entry
point populate the structured JSON fields.
"""

import json
import logging

import pytest
from unittest.mock import AsyncMock, patch

from core.observability.logging import (
    JsonFormatter,
    bind_log_context,
    request_id_var,
    run_id_var,
    user_id_var,
)


@pytest.fixture(autouse=True)
def _reset_log_contextvars():
    """Reset the log contextvars before and after each test.

    These tests bind ids directly in the test's own context (the coroutine is
    awaited, not spawned as a task), so without a reset they'd leak across
    tests and pollute the global formatter state.
    """
    tokens = (
        user_id_var.set(None),
        request_id_var.set(None),
        run_id_var.set(None),
    )
    yield
    user_id_var.set(None)
    request_id_var.set(None)
    run_id_var.set(None)
    del tokens


def _make_record(msg: str = "test") -> logging.LogRecord:
    return logging.LogRecord("test.logger", logging.INFO, "", 0, msg, (), None)


# ---------------------------------------------------------------------------
# bind_log_context helper
# ---------------------------------------------------------------------------


def test_bind_log_context_sets_user_id_only():
    bind_log_context(user_id="user-42")
    assert user_id_var.get() == "user-42"
    # request_id / run_id left untouched (still None)
    assert request_id_var.get() is None
    assert run_id_var.get() is None


def test_bind_log_context_sets_all_ids():
    bind_log_context(user_id="user-1", request_id="req-1", run_id="run-1")
    assert user_id_var.get() == "user-1"
    assert request_id_var.get() == "req-1"
    assert run_id_var.get() == "run-1"


def test_bind_log_context_omitted_args_do_not_overwrite():
    request_id_var.set("preexisting-req")
    bind_log_context(user_id="user-9")  # request_id omitted
    assert user_id_var.get() == "user-9"
    # The omitted request_id must NOT be clobbered to None.
    assert request_id_var.get() == "preexisting-req"


def test_json_formatter_includes_bound_run_id():
    """run_id appears in the JSON output once bound (and is absent otherwise)."""
    formatter = JsonFormatter()
    # Before binding: run_id field is omitted entirely (HTTP-request shape).
    assert "run_id" not in json.loads(formatter.format(_make_record()))
    bind_log_context(user_id="u1", run_id="run-77")
    data = json.loads(formatter.format(_make_record()))
    assert data["user_id"] == "u1"
    assert data["run_id"] == "run-77"


@pytest.mark.asyncio
async def test_provision_background_task_binds_owner_id_into_logs():
    """dispatch_provision (the SFN executor) does the bind_log_context and
    logs its own failure path. Assert the bound owner_id reaches its
    logger's records."""
    from core.services import provision_dispatch

    formatter = JsonFormatter()
    captured: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(formatter.format(record))

    handler = _CaptureHandler()
    prov_logger = logging.getLogger(provision_dispatch.__name__)
    prov_logger.addHandler(handler)
    prev_level = prov_logger.level
    prov_logger.setLevel(logging.INFO)

    try:
        with (
            # Force dispatch_provision's outer except branch so the
            # exception log line is emitted from inside the task body
            # (after the bind_log_context call).
            patch.object(
                provision_dispatch.container_repo,
                "get_by_owner_id",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            # Never raises (catches + logs).
            await provision_dispatch.dispatch_provision("org_corr_owner")
    finally:
        prov_logger.removeHandler(handler)
        prov_logger.setLevel(prev_level)

    assert captured, "expected the failure-path log record from dispatch_provision"
    records = [json.loads(line) for line in captured]
    assert all(r["user_id"] == "org_corr_owner" for r in records)
