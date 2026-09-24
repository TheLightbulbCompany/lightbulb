"""``disarm_owner_nightly_automation`` — stop a de-entitled owner's Paperclip
automation.

Teardown deletes the container but Paperclip's scheduler fires an agent's
timer heartbeat while ``runtimeConfig.heartbeat.enabled`` holds on an active
company, and its heartbeat runner retry-loops ``in_progress`` issues. So a
cancelled owner left armed reproduces the storm where every wake hits the
deleted container. This turns off every starter agent's heartbeat and cancels
the in-progress issues that would loop.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


def _admin(*, issue_pages):
    admin = MagicMock()
    admin.set_agent_heartbeat = AsyncMock(return_value={})
    admin.list_issues = AsyncMock(side_effect=list(issue_pages))
    admin.patch_issue = AsyncMock(return_value={})
    return admin


def _ws(starter_agent_ids=None, seeded_agent_ids=None):
    return MagicMock(company_id="company", starter_agent_ids=starter_agent_ids, seeded_agent_ids=seeded_agent_ids)


def _patches(admin, ws):
    http = MagicMock()
    http.aclose = AsyncMock()
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=ws)
    return (
        patch.object(lifecycle, "PaperclipRepo", return_value=repo),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
    )


async def _disarm(admin, ws=None, **extra):
    ws = ws if ws is not None else _ws({"ceo": "ceo1", "engineering": "eng1"})
    p1, p2, p3, p4 = _patches(admin, ws)
    with p1, p2, p3, p4:
        for p in extra.values():
            p.start()
        try:
            return await lifecycle.disarm_owner_nightly_automation("owner")
        finally:
            for p in extra.values():
                p.stop()


@pytest.mark.asyncio
async def test_disables_every_starter_heartbeat_and_cancels_in_progress_issues():
    admin = _admin(issue_pages=[[{"id": "exec-issue", "status": "in_progress"}, {"id": "w", "status": "in_progress"}]])
    result = await _disarm(admin)

    assert result == {"heartbeats_disabled": 2, "issues_cancelled": 2}
    assert [(c.kwargs["agent_id"], c.kwargs["heartbeat"]) for c in admin.set_agent_heartbeat.await_args_list] == [
        ("ceo1", {"enabled": False}),
        ("eng1", {"enabled": False}),
    ]
    assert [c.kwargs["issue_id"] for c in admin.patch_issue.await_args_list] == ["exec-issue", "w"]
    assert all(c.kwargs["body"] == {"status": "cancelled"} for c in admin.patch_issue.await_args_list)


@pytest.mark.asyncio
async def test_legacy_roster_disables_the_first_seeded_agent():
    admin = _admin(issue_pages=[[]])
    result = await _disarm(admin, _ws(seeded_agent_ids=["friday1", "atlas1"]))
    assert result == {"heartbeats_disabled": 1, "issues_cancelled": 0}
    admin.set_agent_heartbeat.assert_awaited_once_with(
        session_cookie="cookie", agent_id="friday1", heartbeat={"enabled": False}
    )


@pytest.mark.asyncio
async def test_leaves_terminal_issues_alone():
    admin = _admin(
        issue_pages=[
            [
                {"id": "done", "status": "done"},
                {"id": "cancelled", "status": "cancelled"},
                {"id": "live", "status": "in_progress"},
            ]
        ],
    )
    result = await _disarm(admin)

    assert result["issues_cancelled"] == 1
    admin.patch_issue.assert_awaited_once_with(issue_id="live", body={"status": "cancelled"}, session_cookie="cookie")


@pytest.mark.asyncio
async def test_no_company_is_a_no_op():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=None)
    with patch.object(lifecycle, "PaperclipRepo", return_value=repo):
        result = await lifecycle.disarm_owner_nightly_automation("owner")
    assert result == {"heartbeats_disabled": 0, "issues_cancelled": 0}


@pytest.mark.asyncio
async def test_snapshots_all_issue_pages_before_cancelling():
    # Two full pages then a short one: all in_progress issues must be collected
    # BEFORE any cancel, or offset paging skips rows as the filtered set shrinks.
    admin = _admin(
        issue_pages=[
            [{"id": "a", "status": "in_progress"}, {"id": "b", "status": "in_progress"}],
            [{"id": "c", "status": "in_progress"}],
        ],
    )
    result = await _disarm(admin, page=patch.object(lifecycle, "_ISSUE_PAGE_SIZE", 2))

    assert result["issues_cancelled"] == 3
    assert [c.kwargs["params"]["offset"] for c in admin.list_issues.await_args_list] == ["0", "2"]
