"""Tests for the update_service scheduled worker — focused on the
T13 Paperclip passes.

Coverage:
* ``_paperclip_purge_pass`` calls ``provisioning.purge(org_id=...)`` for
  every due workspace row and swallows per-row errors.
* ``_paperclip_provision_retry_pass`` dispatches ``op="archive_member"``
  (the only live op after Tasks 6+7 stripped eager provisioning),
  marks rows applied on success, marks rows failed on non-retryable
  errors, and leaves them pending on retryable errors.
* The scheduled worker loop runs both Paperclip passes once per
  iteration (purge gated by daily cadence).

Strategy:
* The Paperclip provisioning service + httpx client are stubbed out
  via ``_build_paperclip_provisioning`` patching, so no external
  Paperclip is contacted.
* ``update_repo`` calls (``list_pending_by_type``, ``mark_applied``,
  ``mark_failed``) are patched as ``AsyncMock`` so the test asserts
  on call shape without needing moto.
* ``asyncio_mode = "auto"`` (pyproject.toml) is in effect — async
  tests need no decorator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import update_service


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_provisioning_mock() -> MagicMock:
    """A MagicMock with AsyncMock methods matching PaperclipProvisioning."""
    prov = MagicMock()
    prov.archive_member = AsyncMock(return_value=None)
    prov.purge = AsyncMock(return_value=None)
    return prov


def _make_http_mock() -> MagicMock:
    http = MagicMock()
    http.aclose = AsyncMock(return_value=None)
    return http


def _make_repo_mock(due_rows: list | None = None) -> MagicMock:
    repo = MagicMock()
    repo.scan_purge_due = AsyncMock(return_value=due_rows or [])
    return repo


class _FakeRetryable(Exception):
    retryable = True


class _FakeNonRetryable(Exception):
    retryable = False


# ----------------------------------------------------------------------
# _paperclip_purge_pass
# ----------------------------------------------------------------------


async def test_purge_pass_no_due_rows_is_noop():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    repo = _make_repo_mock(due_rows=[])

    with patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, repo)):
        await update_service._paperclip_purge_pass()

    repo.scan_purge_due.assert_awaited_once()
    prov.purge.assert_not_awaited()
    http.aclose.assert_awaited_once()  # always closed in finally


async def test_purge_pass_calls_purge_for_each_due_row():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        MagicMock(org_id="org_a"),
        MagicMock(org_id="org_b"),
        MagicMock(org_id="org_c"),
    ]
    repo = _make_repo_mock(due_rows=rows)

    with patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, repo)):
        await update_service._paperclip_purge_pass()

    assert prov.purge.await_count == 3
    awaited = [c.kwargs["org_id"] for c in prov.purge.await_args_list]
    assert awaited == ["org_a", "org_b", "org_c"]
    http.aclose.assert_awaited_once()


async def test_purge_pass_swallows_per_row_failure_and_continues():
    prov = _make_provisioning_mock()
    # First row raises; second + third should still get called.
    prov.purge = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
    http = _make_http_mock()
    rows = [
        MagicMock(org_id="org_a"),
        MagicMock(org_id="org_b"),
        MagicMock(org_id="org_c"),
    ]
    repo = _make_repo_mock(due_rows=rows)

    with patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, repo)):
        await update_service._paperclip_purge_pass()  # must not raise

    assert prov.purge.await_count == 3
    http.aclose.assert_awaited_once()


async def test_purge_pass_scan_failure_still_closes_http():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    repo = MagicMock()
    repo.scan_purge_due = AsyncMock(side_effect=RuntimeError("ddb down"))

    with patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, repo)):
        await update_service._paperclip_purge_pass()  # must not raise

    prov.purge.assert_not_awaited()
    http.aclose.assert_awaited_once()


async def test_purge_pass_build_failure_is_logged_and_returns():
    """If we can't even build the provisioning service we must not crash."""
    with patch.object(
        update_service,
        "_build_paperclip_provisioning",
        side_effect=RuntimeError("config missing"),
    ):
        await update_service._paperclip_purge_pass()  # must not raise


# ----------------------------------------------------------------------
# _paperclip_provision_retry_pass
# ----------------------------------------------------------------------


async def test_retry_pass_no_pending_rows_is_noop():
    prov = _make_provisioning_mock()
    http = _make_http_mock()

    with (
        patch.object(
            update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=[])
        ) as list_pending,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())) as build,
    ):
        await update_service._paperclip_provision_retry_pass()

    list_pending.assert_awaited_once_with("paperclip_provision")
    # No pending rows -> we must not even build the provisioning service
    # (cheaper hot path) and no http client is opened.
    build.assert_not_called()


async def test_retry_pass_dispatches_archive_member_on_success():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u_archive",
            "changes": {"op": "archive_member", "user_id": "user_a", "org_id": "org_acme"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.archive_member.assert_awaited_once_with(user_id="user_a", org_id="org_acme")
    mark_applied.assert_awaited_once_with("user_a", "u_archive")
    mark_failed.assert_not_awaited()
    http.aclose.assert_awaited_once()


async def test_retry_pass_unknown_op_marks_failed():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u4",
            "changes": {"op": "wat"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.archive_member.assert_not_awaited()
    mark_applied.assert_not_awaited()
    mark_failed.assert_awaited_once()
    args, kwargs = mark_failed.call_args
    # signature is mark_failed(owner_id, update_id, reason)
    assert args[0] == "user_a"
    assert args[1] == "u4"
    assert "wat" in (kwargs.get("reason") or args[2])


async def test_retry_pass_retryable_failure_leaves_row_pending():
    """Retryable errors must NOT be marked failed — they stay pending so
    the next iteration re-tries them."""
    prov = _make_provisioning_mock()
    prov.archive_member = AsyncMock(side_effect=_FakeRetryable("upstream 503"))
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u5",
            "changes": {"op": "archive_member", "user_id": "user_a", "org_id": "org_acme"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.archive_member.assert_awaited_once()
    mark_applied.assert_not_awaited()
    mark_failed.assert_not_awaited()  # crucial: row stays pending


async def test_retry_pass_nonretryable_failure_marks_failed():
    prov = _make_provisioning_mock()
    prov.archive_member = AsyncMock(side_effect=_FakeNonRetryable("bad user"))
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u6",
            "changes": {"op": "archive_member", "user_id": "user_a", "org_id": "org_acme"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    mark_applied.assert_not_awaited()
    mark_failed.assert_awaited_once()
    args, _kwargs = mark_failed.call_args
    assert args[0] == "user_a"
    assert args[1] == "u6"


async def test_retry_pass_legacy_payload_without_org_id_marks_failed():
    """A payload queued before archive_member became org-scoped carries no
    org_id. It must be marked failed (non-retryable) and never archive the
    user from an arbitrary workspace."""
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u_legacy",
            "changes": {"op": "archive_member", "user_id": "user_a"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.archive_member.assert_not_awaited()
    mark_applied.assert_not_awaited()
    mark_failed.assert_awaited_once()
    args, _kwargs = mark_failed.call_args
    assert args[:2] == ("user_a", "u_legacy")


async def test_retry_pass_processes_multiple_rows_independently():
    """Mix of success / retryable / non-retryable in a single batch."""
    prov = _make_provisioning_mock()
    # row 1 success, row 2 retryable, row 3 non-retryable.
    prov.archive_member = AsyncMock(side_effect=[None, _FakeRetryable("503"), _FakeNonRetryable("bad")])
    http = _make_http_mock()
    rows = [
        {
            "owner_id": f"user_{i}",
            "update_id": f"u_{i}",
            "changes": {"op": "archive_member", "user_id": f"user_{i}", "org_id": "org_acme"},
        }
        for i in range(3)
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    assert prov.archive_member.await_count == 3
    mark_applied.assert_awaited_once_with("user_0", "u_0")  # row 1 only
    mark_failed.assert_awaited_once()  # row 3 only — row 2 stays pending
    failed_args, _ = mark_failed.call_args
    assert failed_args[0] == "user_2"
    assert failed_args[1] == "u_2"


async def test_retry_pass_list_failure_does_not_raise():
    """If we can't list pending, we log + return — never crash the loop."""
    with patch.object(
        update_service.update_repo,
        "list_pending_by_type",
        new=AsyncMock(side_effect=RuntimeError("ddb down")),
    ):
        await update_service._paperclip_provision_retry_pass()


# ----------------------------------------------------------------------
# run_scheduled_worker integration
# ----------------------------------------------------------------------


async def test_run_scheduled_worker_invokes_both_paperclip_passes_per_iteration(
    monkeypatch,
):
    """Single-iteration smoke test: the worker loop calls both passes
    on its first iteration.

    We patch out the interruptible inter-iteration wait (``asyncio.wait_for``) to break the
    loop on the first call. The fourth pass it used to also drive -- applying due Track-2
    container updates (image rolls, resizes, task-definition refreshes) and recovering their
    stranded claims -- went with the ECS runtime.
    """
    purge_calls = 0
    retry_calls = 0

    async def fake_purge():
        nonlocal purge_calls
        purge_calls += 1

    async def fake_retry():
        nonlocal retry_calls
        retry_calls += 1

    class _StopLoop(Exception):
        pass

    async def stop_after_first(*_args, **_kwargs):
        raise _StopLoop()

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_paperclip_provision_retry_pass", fake_retry)
    monkeypatch.setattr(update_service, "_rollup_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", AsyncMock(return_value=True))
    monkeypatch.setattr(update_service.asyncio, "wait_for", stop_after_first)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert retry_calls == 1, "retry pass must run every iteration it wins admission"
    assert purge_calls == 1, "purge pass runs when admitted (cadence lives on the admission row now)"


async def test_run_scheduled_worker_skips_purge_within_24h(monkeypatch):
    """On the *second* iteration purge should NOT run: admission refuses it.

    Cadence now lives on the try_claim_pass row, not a process clock -- stub
    it to admit the purge pass once, then refuse it, while every other pass
    (retry) is admitted every iteration.
    """
    purge_calls = 0
    retry_calls = 0
    wait_count = 0

    async def fake_purge():
        nonlocal purge_calls
        purge_calls += 1

    async def fake_retry():
        nonlocal retry_calls
        retry_calls += 1

    class _StopLoop(Exception):
        pass

    async def fake_wait(*_args, **_kwargs):
        nonlocal wait_count
        wait_count += 1
        if wait_count >= 2:
            # Stop after second iteration completes.
            raise _StopLoop()

    # First check admits the purge pass; the second (same 24h window) refuses it.
    purge_admissions = iter([True, False])

    async def fake_try_claim_pass(name, *, interval_s):
        if name == "paperclip_purge":
            return next(purge_admissions)
        return True  # every other pass (retry, due_updates, rollup) wins every tick

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_paperclip_provision_retry_pass", fake_retry)
    monkeypatch.setattr(update_service, "_rollup_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", fake_try_claim_pass)
    monkeypatch.setattr(update_service.asyncio, "wait_for", fake_wait)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert retry_calls == 2, "retry pass wins admission every loop iteration"
    assert purge_calls == 1, "purge pass runs only when admission wins its window"


async def test_run_scheduled_worker_rollup_pass_hourly_cadence(monkeypatch):
    """Rollup pass runs when admitted, is skipped when admission refuses it,
    and runs again once admission wins again."""
    rollup_calls = 0

    async def fake_rollup():
        nonlocal rollup_calls
        rollup_calls += 1

    class _StopLoop(Exception):
        pass

    wait_count = 0

    async def fake_wait(*_args, **_kwargs):
        nonlocal wait_count
        wait_count += 1
        if wait_count >= 3:
            raise _StopLoop()

    # Iter 1: admitted -> runs. Iter 2: refused -> skipped. Iter 3: admitted again -> runs.
    rollup_admissions = iter([True, False, True])

    async def fake_try_claim_pass(name, *, interval_s):
        if name == "owner_day_rollup":
            return next(rollup_admissions)
        return True

    monkeypatch.setattr(update_service, "_rollup_pass", fake_rollup)
    monkeypatch.setattr(update_service, "_paperclip_purge_pass", AsyncMock())
    monkeypatch.setattr(update_service, "_paperclip_provision_retry_pass", AsyncMock())
    monkeypatch.setattr(update_service.container_repo, "try_claim_pass", fake_try_claim_pass)
    monkeypatch.setattr(update_service.asyncio, "wait_for", fake_wait)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert rollup_calls == 2, "rollup pass: first admitted iteration + after admission wins again"


# ----------------------------------------------------------------------
# update_repo extension methods (sanity)
# ----------------------------------------------------------------------


def test_update_repo_exposes_t13_methods():
    """Smoke test that the repo methods T13 depends on actually exist.

    Saves a confusing AttributeError at runtime if a future refactor
    renames or removes one of these.
    """
    from core.repositories import update_repo as ur

    assert callable(ur.list_pending_by_type)
    assert callable(ur.mark_applied)
    assert callable(ur.mark_failed)


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
