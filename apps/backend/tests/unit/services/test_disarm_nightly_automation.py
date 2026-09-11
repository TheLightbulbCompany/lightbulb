"""``disarm_owner_nightly_automation`` — stop a de-entitled owner's Paperclip
automation.

Teardown deletes the container but the Paperclip scheduler fires a routine
purely on ``routines.status='active'`` + ``routine_triggers.enabled`` (no
company-disabled gate), and its heartbeat runner retry-loops ``in_progress``
issues. So a cancelled owner whose routine stays active reproduces the storm
where every fire hits the deleted container. This pauses the nightly CEO
routine and cancels the in-progress issues that would loop.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle
from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE


def _admin(*, routines, issue_pages):
    admin = MagicMock()
    admin.list_routines = AsyncMock(return_value=routines)
    admin.patch_routine = AsyncMock(return_value={})
    admin.list_issues = AsyncMock(side_effect=list(issue_pages))
    admin.patch_issue = AsyncMock(return_value={})
    return admin


def _patches(admin):
    http = MagicMock()
    http.aclose = AsyncMock()
    # Stashed on ``admin`` (a MagicMock) purely as a handle for assertions —
    # patch.object binds this same object onto billing_repo.
    admin.release_nightly_arm = AsyncMock()
    return (
        patch.object(lifecycle, "resolve_company_id_for_owner", new=AsyncMock(return_value="company")),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
        patch.object(lifecycle.billing_repo, "release_nightly_arm", new=admin.release_nightly_arm),
    )


@pytest.mark.asyncio
async def test_pauses_active_nightly_routine_and_cancels_in_progress_issues():
    admin = _admin(
        routines=[
            {"id": "nightly", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "active"},
        ],
        issue_pages=[
            [
                {"id": "exec-issue", "status": "in_progress"},
                {"id": "watchdog", "status": "in_progress"},
            ]
        ],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    with p1, p2, p3, p4, p5:
        result = await lifecycle.disarm_owner_nightly_automation("owner")

    assert result == {"routines_paused": 1, "issues_cancelled": 2}
    admin.patch_routine.assert_awaited_once_with(
        routine_id="nightly", body={"status": "paused"}, session_cookie="cookie"
    )
    assert [c.kwargs["issue_id"] for c in admin.patch_issue.await_args_list] == ["exec-issue", "watchdog"]
    assert all(c.kwargs["body"] == {"status": "cancelled"} for c in admin.patch_issue.await_args_list)


@pytest.mark.asyncio
async def test_skips_non_nightly_routines_and_already_paused_nightly():
    admin = _admin(
        routines=[
            {"id": "custom", "title": "Weekly digest", "status": "active"},
            {"id": "already", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "paused"},
            {"id": "archived", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "archived"},
        ],
        issue_pages=[[]],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    with p1, p2, p3, p4, p5:
        result = await lifecycle.disarm_owner_nightly_automation("owner")

    assert result == {"routines_paused": 0, "issues_cancelled": 0}
    admin.patch_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaves_terminal_issues_alone():
    admin = _admin(
        routines=[],
        issue_pages=[
            [
                {"id": "done", "status": "done"},
                {"id": "cancelled", "status": "cancelled"},
                {"id": "live", "status": "in_progress"},
            ]
        ],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    with p1, p2, p3, p4, p5:
        result = await lifecycle.disarm_owner_nightly_automation("owner")

    assert result == {"routines_paused": 0, "issues_cancelled": 1}
    admin.patch_issue.assert_awaited_once_with(issue_id="live", body={"status": "cancelled"}, session_cookie="cookie")


@pytest.mark.asyncio
async def test_no_company_is_a_no_op():
    with patch.object(lifecycle, "resolve_company_id_for_owner", new=AsyncMock(return_value=None)):
        result = await lifecycle.disarm_owner_nightly_automation("owner")
    assert result == {"routines_paused": 0, "issues_cancelled": 0}


@pytest.mark.asyncio
async def test_snapshots_all_issue_pages_before_cancelling():
    # Two full pages then a short one: all in_progress issues must be collected
    # BEFORE any cancel, or offset paging skips rows as the filtered set shrinks.
    admin = _admin(
        routines=[],
        issue_pages=[
            [{"id": "a", "status": "in_progress"}, {"id": "b", "status": "in_progress"}],
            [{"id": "c", "status": "in_progress"}],
        ],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    with p1, p2, p3, p4, p5, patch.object(lifecycle, "_ISSUE_PAGE_SIZE", 2):
        result = await lifecycle.disarm_owner_nightly_automation("owner")

    assert result == {"routines_paused": 0, "issues_cancelled": 3}
    assert [c.kwargs["params"]["offset"] for c in admin.list_issues.await_args_list] == ["0", "2"]


@pytest.mark.asyncio
async def test_releases_the_arm_cache_so_a_resubscribe_reaches_the_reconcile():
    # The poll-path arm short-circuits on nightly_ceo_armed_company; if teardown
    # pauses the routine but leaves that cache set, a resubscribe whose container
    # SURVIVED teardown's best-effort delete never reaches reconcile_paused and
    # the routine stays paused. Disarm must release the cache for that company.
    admin = _admin(
        routines=[{"id": "nightly", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "active"}],
        issue_pages=[[]],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    with p1, p2, p3, p4, p5:
        await lifecycle.disarm_owner_nightly_automation("owner")

    admin.release_nightly_arm.assert_awaited_once_with("owner", "company")


@pytest.mark.asyncio
async def test_arm_cache_release_failure_does_not_undo_the_pause():
    # The routine pause and issue cancel are the storm-stoppers; a billing-write
    # hiccup on the cache release must be swallowed, not lose them.
    admin = _admin(
        routines=[{"id": "nightly", "title": NIGHTLY_CEO_ROUTINE_TITLE, "status": "active"}],
        issue_pages=[[{"id": "live", "status": "in_progress"}]],
    )
    p1, p2, p3, p4, p5 = _patches(admin)
    admin.release_nightly_arm.side_effect = RuntimeError("ddb down")
    with p1, p2, p3, p4, p5:
        result = await lifecycle.disarm_owner_nightly_automation("owner")

    assert result == {"routines_paused": 1, "issues_cancelled": 1}
    admin.patch_routine.assert_awaited_once()
