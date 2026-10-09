"""``pause_owner_automation`` / ``resume_owner_automation`` / ``disarm_owner_nightly_automation``.

Paperclip's scheduler fires a routine while it is ``active`` and an agent's timer heartbeat
while ``runtimeConfig.heartbeat.enabled`` holds -- neither checks for a runtime, so a cancelled
workspace kept filing a BLOCKED task every night. The pause turns both off and records what it
took; the resume (on re-entitlement) puts back exactly that, never what the owner paused.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle
from core.services.paperclip_admin_client import PaperclipApiError

AGENTS = [
    {"id": "ceo1", "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 28800}}},
    {"id": "legacy-spec", "runtimeConfig": {"heartbeat": {"enabled": True}}},
    {"id": "off", "runtimeConfig": {"heartbeat": {"enabled": False}}},
    {"id": "none", "runtimeConfig": {}},
]
ROUTINES = [
    {"id": "r-live", "status": "active"},
    {"id": "r-owner-paused", "status": "paused"},
    {"id": "r-archived", "status": "archived"},
]


def _admin(*, issue_pages=((),)):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=AGENTS)
    admin.list_routines = AsyncMock(return_value=ROUTINES)
    admin.set_agent_heartbeat = AsyncMock(return_value={})
    admin.patch_routine = AsyncMock(return_value={})
    admin.get_routine = AsyncMock(return_value={"status": "paused"})
    admin.list_issues = AsyncMock(side_effect=[list(p) for p in issue_pages])
    admin.patch_issue = AsyncMock(return_value={})
    return admin


def _ws(**kw):
    defaults = dict(
        company_id="company",
        automation_paused_at=None,
        automation_paused_agent_ids=None,
        automation_paused_routine_ids=None,
    )
    return MagicMock(**{**defaults, **kw})


def _repo(ws):
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=ws)
    repo.record_paused_automation = AsyncMock()
    repo.clear_paused_automation = AsyncMock()
    repo.mark_pause_complete = AsyncMock()
    return repo


async def _call(fn, admin, ws, *args, repo=None, **kwargs):
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    repo = repo or _repo(ws)
    with (
        patch.object(lifecycle, "PaperclipRepo", return_value=repo),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
        patch.object(lifecycle, "put_metric"),
    ):
        return await fn("owner", *args, **kwargs), repo


@pytest.mark.asyncio
async def test_cancel_pauses_active_routines_and_every_enabled_heartbeat_recording_them_first():
    admin = _admin()
    result, repo = await _call(lifecycle.pause_owner_automation, admin, _ws())

    assert result == {"agent_ids": ["ceo1", "legacy-spec"], "routine_ids": ["r-live"]}
    repo.record_paused_automation.assert_awaited_once_with(
        "owner", agent_ids=["ceo1", "legacy-spec"], routine_ids=["r-live"]
    )
    assert [(c.kwargs["agent_id"], c.kwargs["heartbeat"]) for c in admin.set_agent_heartbeat.await_args_list] == [
        ("ceo1", {"enabled": False}),
        ("legacy-spec", {"enabled": False}),
    ]
    admin.patch_routine.assert_awaited_once_with(
        session_cookie="cookie", routine_id="r-live", body={"status": "paused"}
    )
    repo.mark_pause_complete.assert_awaited_once_with("owner")


@pytest.mark.asyncio
async def test_a_partial_pause_keeps_its_record_but_is_not_marked_complete():
    admin = _admin()
    admin.patch_routine.side_effect = RuntimeError("paperclip down")
    ws = _ws()
    repo = _repo(ws)
    with pytest.raises(RuntimeError):
        await _call(lifecycle.pause_owner_automation, admin, ws, repo=repo)
    repo.record_paused_automation.assert_awaited_once()  # ids recorded first, so resume restores them
    repo.mark_pause_complete.assert_not_awaited()  # so the expired-trial sweep retries


@pytest.mark.asyncio
async def test_pause_dry_run_writes_nothing():
    admin = _admin()
    result, repo = await _call(lifecycle.pause_owner_automation, admin, _ws(), dry_run=True)
    assert result["routine_ids"] == ["r-live"]
    repo.record_paused_automation.assert_not_awaited()
    admin.set_agent_heartbeat.assert_not_awaited()
    admin.patch_routine.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_without_a_company_is_a_no_op():
    admin = _admin()
    result, repo = await _call(lifecycle.pause_owner_automation, admin, None)
    assert result == {"agent_ids": [], "routine_ids": []}
    admin.list_agents.assert_not_awaited()


@pytest.mark.asyncio
async def test_re_entitle_resumes_exactly_what_the_pause_took():
    admin = _admin()
    admin.get_routine = AsyncMock(side_effect=[{"status": "paused"}, {"status": "archived"}])
    ws = _ws(
        automation_paused_at="2026-10-09T00:00:00Z",
        automation_paused_agent_ids={"ceo1", "gone"},
        automation_paused_routine_ids={"r-a", "r-b"},
    )
    admin.set_agent_heartbeat.side_effect = [{}, PaperclipApiError("PATCH -> 404", 404, None)]

    result, repo = await _call(lifecycle.resume_owner_automation, admin, ws)

    assert result == {"heartbeats_resumed": 1, "routines_resumed": 1}
    assert [c.kwargs["heartbeat"] for c in admin.set_agent_heartbeat.await_args_list] == [{"enabled": True}] * 2
    # r-b was archived by the owner while paused: left alone.
    admin.patch_routine.assert_awaited_once_with(session_cookie="cookie", routine_id="r-a", body={"status": "active"})
    repo.clear_paused_automation.assert_awaited_once_with(
        "owner", agent_ids=["ceo1", "gone"], routine_ids=["r-a", "r-b"]
    )


@pytest.mark.asyncio
async def test_resume_with_nothing_recorded_touches_nothing():
    admin = _admin()
    result, repo = await _call(lifecycle.resume_owner_automation, admin, _ws())
    assert result == {"heartbeats_resumed": 0, "routines_resumed": 0}
    admin.set_agent_heartbeat.assert_not_awaited()
    repo.clear_paused_automation.assert_not_awaited()


@pytest.mark.asyncio
async def test_disarm_pauses_then_cancels_every_in_progress_issue_snapshotting_pages_first():
    admin = _admin(
        issue_pages=[
            [{"id": "a", "status": "in_progress"}, {"id": "b", "status": "in_progress"}],
            [{"id": "c", "status": "in_progress"}, {"id": "d", "status": "done"}],
            [],
        ]
    )
    with patch.object(lifecycle, "_ISSUE_PAGE_SIZE", 2):
        result, _ = await _call(lifecycle.disarm_owner_nightly_automation, admin, _ws())

    assert result == {"heartbeats_paused": 2, "routines_paused": 1, "issues_cancelled": 3}
    assert [c.kwargs["params"]["offset"] for c in admin.list_issues.await_args_list] == ["0", "2", "4"]
    assert [c.kwargs["issue_id"] for c in admin.patch_issue.await_args_list] == ["a", "b", "c"]
