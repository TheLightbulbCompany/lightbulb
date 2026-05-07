"""Tests for the update_service scheduled worker — focused on the
T13 Paperclip passes.

Coverage:
* ``_paperclip_purge_pass`` calls ``provisioning.purge`` for every
  due row and swallows per-row errors.
* ``_paperclip_provision_retry_pass`` dispatches by ``op``,
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
    prov.provision_org = AsyncMock(return_value=None)
    prov.provision_member = AsyncMock(return_value=None)
    prov.disable = AsyncMock(return_value=None)
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
        MagicMock(user_id="user_a"),
        MagicMock(user_id="user_b"),
        MagicMock(user_id="user_c"),
    ]
    repo = _make_repo_mock(due_rows=rows)

    with patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, repo)):
        await update_service._paperclip_purge_pass()

    assert prov.purge.await_count == 3
    awaited = [c.kwargs["user_id"] for c in prov.purge.await_args_list]
    assert awaited == ["user_a", "user_b", "user_c"]
    http.aclose.assert_awaited_once()


async def test_purge_pass_swallows_per_row_failure_and_continues():
    prov = _make_provisioning_mock()
    # First row raises; second + third should still get called.
    prov.purge = AsyncMock(side_effect=[RuntimeError("boom"), None, None])
    http = _make_http_mock()
    rows = [
        MagicMock(user_id="user_a"),
        MagicMock(user_id="user_b"),
        MagicMock(user_id="user_c"),
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


async def test_retry_pass_dispatches_provision_org_on_success():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u1",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
                "owner_email": "owner@acme.test",
            },
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.provision_org.assert_awaited_once_with(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    mark_applied.assert_awaited_once_with("user_owner", "u1")
    mark_failed.assert_not_awaited()
    http.aclose.assert_awaited_once()


async def test_retry_pass_dispatches_provision_member_on_success():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_member",
            "update_id": "u2",
            "changes": {
                "op": "provision_member",
                "org_id": "org_acme",
                "user_id": "user_member",
                "email": "member@acme.test",
                "owner_email": "owner@acme.test",
            },
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.provision_member.assert_awaited_once_with(
        owner_id="org_acme",
        user_id="user_member",
        email="member@acme.test",
        owner_email="owner@acme.test",
    )
    mark_applied.assert_awaited_once_with("user_member", "u2")


async def test_retry_pass_dispatches_disable_on_success():
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_a",
            "update_id": "u3",
            "changes": {"op": "disable", "user_id": "user_a"},
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.disable.assert_awaited_once_with(user_id="user_a")
    mark_applied.assert_awaited_once_with("user_a", "u3")


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

    prov.provision_org.assert_not_awaited()
    prov.provision_member.assert_not_awaited()
    prov.disable.assert_not_awaited()
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
    prov.provision_org = AsyncMock(side_effect=_FakeRetryable("upstream 503"))
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u5",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
                "owner_email": "owner@acme.test",
            },
        }
    ]

    with (
        patch.object(update_service.update_repo, "list_pending_by_type", new=AsyncMock(return_value=rows)),
        patch.object(update_service.update_repo, "mark_applied", new=AsyncMock(return_value=True)) as mark_applied,
        patch.object(update_service.update_repo, "mark_failed", new=AsyncMock(return_value=True)) as mark_failed,
        patch.object(update_service, "_build_paperclip_provisioning", return_value=(prov, http, MagicMock())),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.provision_org.assert_awaited_once()
    mark_applied.assert_not_awaited()
    mark_failed.assert_not_awaited()  # crucial: row stays pending


async def test_retry_pass_nonretryable_failure_marks_failed():
    prov = _make_provisioning_mock()
    prov.provision_org = AsyncMock(side_effect=_FakeNonRetryable("bad email"))
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u6",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
                "owner_email": "owner@acme.test",
            },
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
    assert args[0] == "user_owner"
    assert args[1] == "u6"


async def test_retry_pass_processes_multiple_rows_independently():
    """Mix of success / retryable / non-retryable in a single batch."""
    prov = _make_provisioning_mock()
    # row 1 success, row 2 retryable, row 3 non-retryable.
    prov.provision_org = AsyncMock(side_effect=[None, _FakeRetryable("503"), _FakeNonRetryable("bad")])
    http = _make_http_mock()
    rows = [
        {
            "owner_id": f"user_{i}",
            "update_id": f"u_{i}",
            "changes": {
                "op": "provision_org",
                "org_id": "org_x",
                "owner_user_id": f"user_{i}",
                "owner_email": "x@x",
            },
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

    assert prov.provision_org.await_count == 3
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


async def test_retry_pass_resolves_owner_email_from_user_repo_when_missing():
    """F3: When the retry payload has no ``owner_email`` (because F2's
    fix omitted it at enqueue time), the retry pass must look it up
    via ``user_repo`` using ``owner_user_id``."""
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u_resolve",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
                # NOTE: no ``owner_email`` key — F2 omits it.
            },
        }
    ]

    user_repo_get = AsyncMock(return_value={"user_id": "user_owner", "email": "owner@acme.test"})

    with (
        patch.object(
            update_service.update_repo,
            "list_pending_by_type",
            new=AsyncMock(return_value=rows),
        ),
        patch.object(
            update_service.update_repo,
            "mark_applied",
            new=AsyncMock(return_value=True),
        ) as mark_applied,
        patch.object(
            update_service,
            "_build_paperclip_provisioning",
            return_value=(prov, http, MagicMock()),
        ),
        patch("core.repositories.user_repo.get", new=user_repo_get),
    ):
        await update_service._paperclip_provision_retry_pass()

    user_repo_get.assert_awaited_once_with("user_owner")
    prov.provision_org.assert_awaited_once_with(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="owner@acme.test",
    )
    mark_applied.assert_awaited_once_with("user_owner", "u_resolve")


async def test_retry_pass_leaves_row_pending_when_owner_email_unresolvable():
    """F3: If ``user_repo.get`` returns nothing (the user.created
    webhook hasn't landed yet), the retry pass MUST leave the row
    pending — not mark it failed — so a later redelivery can supply
    the email."""
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u_unresolved",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
            },
        }
    ]

    with (
        patch.object(
            update_service.update_repo,
            "list_pending_by_type",
            new=AsyncMock(return_value=rows),
        ),
        patch.object(
            update_service.update_repo,
            "mark_applied",
            new=AsyncMock(return_value=True),
        ) as mark_applied,
        patch.object(
            update_service.update_repo,
            "mark_failed",
            new=AsyncMock(return_value=True),
        ) as mark_failed,
        patch.object(
            update_service,
            "_build_paperclip_provisioning",
            return_value=(prov, http, MagicMock()),
        ),
        # users repo has no row yet for the owner.
        patch("core.repositories.user_repo.get", new=AsyncMock(return_value=None)),
    ):
        await update_service._paperclip_provision_retry_pass()

    prov.provision_org.assert_not_awaited()
    mark_applied.assert_not_awaited()
    mark_failed.assert_not_awaited()  # crucial: row stays pending


async def test_retry_pass_resolves_owner_email_for_provision_member_when_missing():
    """F3: Same re-resolution applies to ``provision_member`` payloads.
    The membership_created webhook now includes ``owner_user_id`` so
    the retry pass can look up the owner's email."""
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_member",
            "update_id": "u_member",
            "changes": {
                "op": "provision_member",
                "org_id": "org_acme",
                "user_id": "user_member",
                "email": "member@acme.test",
                "owner_user_id": "user_owner",
                # No ``owner_email`` — must be re-resolved.
            },
        }
    ]

    user_repo_get = AsyncMock(return_value={"user_id": "user_owner", "email": "owner@acme.test"})

    with (
        patch.object(
            update_service.update_repo,
            "list_pending_by_type",
            new=AsyncMock(return_value=rows),
        ),
        patch.object(
            update_service.update_repo,
            "mark_applied",
            new=AsyncMock(return_value=True),
        ) as mark_applied,
        patch.object(
            update_service,
            "_build_paperclip_provisioning",
            return_value=(prov, http, MagicMock()),
        ),
        patch("core.repositories.user_repo.get", new=user_repo_get),
    ):
        await update_service._paperclip_provision_retry_pass()

    user_repo_get.assert_awaited_once_with("user_owner")
    prov.provision_member.assert_awaited_once_with(
        owner_id="org_acme",
        user_id="user_member",
        email="member@acme.test",
        owner_email="owner@acme.test",
    )
    mark_applied.assert_awaited_once_with("user_member", "u_member")


async def test_retry_pass_prefers_fresh_owner_email_over_cached_payload():
    """Round-4 P2: on retries, the resolver MUST re-resolve the
    owner's email from ``user_repo`` (with Clerk fallback) and prefer
    that over any cached ``owner_email`` baked into the retry payload.

    Why: if the user rotated their email between the original webhook
    and the retry, signing in with the cached value fails
    non-retryably and the retry row gets marked failed — requiring
    manual ops cleanup. Always trying a fresh lookup first lets the
    retry self-heal across email rotations.
    """
    prov = _make_provisioning_mock()
    http = _make_http_mock()
    rows = [
        {
            "owner_id": "user_owner",
            "update_id": "u_rotated",
            "changes": {
                "op": "provision_org",
                "org_id": "org_acme",
                "owner_user_id": "user_owner",
                # Cached value from the original webhook; user has
                # since rotated to ``new@example.com``.
                "owner_email": "old@example.com",
            },
        }
    ]

    user_repo_get = AsyncMock(return_value={"user_id": "user_owner", "email": "new@example.com"})

    with (
        patch.object(
            update_service.update_repo,
            "list_pending_by_type",
            new=AsyncMock(return_value=rows),
        ),
        patch.object(
            update_service.update_repo,
            "mark_applied",
            new=AsyncMock(return_value=True),
        ) as mark_applied,
        patch.object(
            update_service,
            "_build_paperclip_provisioning",
            return_value=(prov, http, MagicMock()),
        ),
        patch("core.repositories.user_repo.get", new=user_repo_get),
    ):
        await update_service._paperclip_provision_retry_pass()

    user_repo_get.assert_awaited_once_with("user_owner")
    # Critically: provision_org received the FRESH email, not the
    # stale cached one from the payload.
    prov.provision_org.assert_awaited_once_with(
        owner_id="org_acme",
        owner_user_id="user_owner",
        owner_email="new@example.com",
    )
    mark_applied.assert_awaited_once_with("user_owner", "u_rotated")


# ----------------------------------------------------------------------
# run_scheduled_worker integration
# ----------------------------------------------------------------------


async def test_run_scheduled_worker_invokes_both_paperclip_passes_per_iteration(
    monkeypatch,
):
    """Single-iteration smoke test: the worker loop calls both passes
    on its first iteration.

    We patch out ``asyncio.sleep`` to break the loop on the first call,
    plus stub ``get_due_scheduled`` so the legacy path is a no-op.
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

    async def stop_after_first(_):
        raise _StopLoop()

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_paperclip_provision_retry_pass", fake_retry)
    monkeypatch.setattr(
        update_service.update_repo,
        "get_due_scheduled",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(update_service.asyncio, "sleep", stop_after_first)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert retry_calls == 1, "retry pass must run every iteration"
    assert purge_calls == 1, "purge pass must run on first iteration (last_purge_pass_at=0)"


async def test_run_scheduled_worker_skips_purge_within_24h(monkeypatch):
    """On the *second* iteration purge should NOT run (still within 24h).

    Strategy: monkeypatch ``time.monotonic`` so the elapsed delta on the
    second iteration is < 24h, then stop after two iterations.
    """
    purge_calls = 0
    retry_calls = 0
    sleep_count = 0

    async def fake_purge():
        nonlocal purge_calls
        purge_calls += 1

    async def fake_retry():
        nonlocal retry_calls
        retry_calls += 1

    class _StopLoop(Exception):
        pass

    async def fake_sleep(_):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            # Stop after second iteration completes.
            raise _StopLoop()

    # First iteration: now - last_purge_pass_at (=0) must be >= 24h so
    # purge runs and last_purge_pass_at gets set. Second iteration:
    # now - last_purge_pass_at must be < 24h so purge is skipped.
    interval = update_service._PURGE_PASS_INTERVAL_SECONDS
    monotonic_values = iter(
        [
            interval + 100.0,  # iter 1: triggers purge, sets last_purge_pass_at = this
            interval + 200.0,  # iter 2: delta=100s, well below interval -> skip
            interval + 300.0,
            interval + 400.0,
            interval + 500.0,
        ]
    )

    def fake_monotonic():
        try:
            return next(monotonic_values)
        except StopIteration:
            return interval + 1000.0

    monkeypatch.setattr(update_service, "_paperclip_purge_pass", fake_purge)
    monkeypatch.setattr(update_service, "_paperclip_provision_retry_pass", fake_retry)
    monkeypatch.setattr(
        update_service.update_repo,
        "get_due_scheduled",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(update_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(update_service.time, "monotonic", fake_monotonic)

    with pytest.raises(_StopLoop):
        await update_service.run_scheduled_worker()

    assert retry_calls == 2, "retry pass runs every loop iteration"
    assert purge_calls == 1, "purge pass runs only on first iteration"


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
