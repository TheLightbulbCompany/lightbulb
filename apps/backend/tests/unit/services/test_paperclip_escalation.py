"""Escalation of platform-failed, recovery-blocked Paperclip issues."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_escalation as esc
from core.services.paperclip_admin_client import PaperclipApiError

OWNER = "org_1"
X, ENG, CEO, FORGE = "agent-x", "agent-eng", "agent-ceo", "agent-forge"


def _agents(**status):
    rows = [
        {"id": X, "name": "Growth", "role": "general", "reportsTo": CEO},
        {"id": ENG, "name": "Engineering", "role": "engineer", "reportsTo": CEO},
        {"id": CEO, "name": "CEO", "role": "ceo", "reportsTo": None},
        {"id": FORGE, "name": "Forge", "role": "engineer", "reportsTo": CEO},
    ]
    for row in rows:
        row["status"] = status.get(row["id"], "idle")
    return {"agents": rows}


@pytest.fixture
def env(monkeypatch):
    admin = MagicMock()
    admin.get_issue = AsyncMock(return_value={"status": "blocked", "assigneeAgentId": CEO})
    admin.list_agents = AsyncMock(return_value=_agents())
    admin.patch_issue = AsyncMock(return_value={})
    admin.add_issue_comment = AsyncMock(return_value={})
    metric = MagicMock()
    monkeypatch.setattr(esc.settings, "PAPERCLIP_ESCALATION_AGENT_OVERRIDES", {})
    with (
        patch.object(esc, "_resolve_owner", AsyncMock(return_value=(OWNER, {"engineering": ENG, "ceo": CEO}))),
        patch.object(esc, "PaperclipAdminClient", return_value=admin),
        patch.object(esc, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(esc, "put_metric", metric),
    ):
        yield admin, metric


def _patched_to(admin):
    admin.patch_issue.assert_awaited_once()
    body = admin.patch_issue.await_args.kwargs["body"]
    assert body["status"] == "todo"
    return body["assigneeAgentId"]


async def test_specialist_failure_goes_to_engineering(env):
    admin, metric = env
    await esc.escalate("co", "iss", X, "setup_failed")
    assert _patched_to(admin) == ENG
    metric.assert_called_once_with("paperclip.escalation.escalated", dimensions={"rung": "escalation"})
    comment = admin.add_issue_comment.await_args.kwargs["body"]["body"]
    assert comment.startswith("Escalated from Growth: its run failed twice with setup_failed")
    assert comment.endswith("Reassigned to Engineering.")


async def test_override_wins_over_engineering(env, monkeypatch):
    admin, _ = env
    monkeypatch.setattr(esc.settings, "PAPERCLIP_ESCALATION_AGENT_OVERRIDES", {OWNER: FORGE})
    await esc.escalate("co", "iss", X, "process_lost")
    assert _patched_to(admin) == FORGE


async def test_escalation_agent_failure_goes_to_ceo_even_if_already_assigned(env):
    """Recovery already handed it to the CEO (Engineering's manager); unblocking is the point."""
    admin, metric = env
    await esc.escalate("co", "iss", ENG, "timeout")
    assert _patched_to(admin) == CEO
    metric.assert_called_once_with("paperclip.escalation.escalated", dimensions={"rung": "ceo"})


async def test_no_engineering_falls_back_to_ceo(env):
    admin, _ = env
    esc._resolve_owner.return_value = (OWNER, {})
    await esc.escalate("co", "iss", X, "setup_failed")
    assert _patched_to(admin) == CEO


async def test_paused_escalation_agent_falls_through_to_ceo(env):
    admin, _ = env
    admin.list_agents.return_value = _agents(**{ENG: "paused"})
    await esc.escalate("co", "iss", X, "setup_failed")
    assert _patched_to(admin) == CEO


@pytest.mark.parametrize("failed", [CEO, ENG])
async def test_exhausted_leaves_it_blocked(env, failed):
    admin, metric = env
    if failed == ENG:  # Engineering failed and the CEO cannot run
        admin.list_agents.return_value = _agents(**{CEO: "terminated"})
    await esc.escalate("co", "iss", failed, "setup_failed")
    admin.patch_issue.assert_not_awaited()
    metric.assert_called_once_with("paperclip.escalation.exhausted")


@pytest.mark.parametrize(
    "issue",
    [
        {"status": "todo", "assigneeAgentId": X},
        {"status": "blocked", "assigneeUserId": "user_1", "assigneeAgentId": None},
    ],
)
async def test_not_blocked_or_human_assigned_is_left_alone(env, issue):
    admin, metric = env
    admin.get_issue.return_value = issue
    await esc.escalate("co", "iss", X, "setup_failed")
    admin.patch_issue.assert_not_awaited()
    metric.assert_not_called()


async def test_422_is_refused_and_not_commented(env):
    admin, metric = env
    admin.patch_issue.side_effect = PaperclipApiError("PATCH -> 422", 422, {"error": "blockers"})
    await esc.escalate("co", "iss", X, "setup_failed")
    metric.assert_called_once_with("paperclip.escalation.refused")
    admin.add_issue_comment.assert_not_awaited()


async def test_comment_failure_keeps_the_reassignment(env):
    admin, metric = env
    admin.add_issue_comment.side_effect = RuntimeError("boom")
    await esc.escalate("co", "iss", X, "setup_failed")  # does not raise
    assert _patched_to(admin) == ENG
    metric.assert_called_once_with("paperclip.escalation.escalated", dimensions={"rung": "escalation"})


def _failed(code, issue_id="iss"):
    return {
        "eventType": "agent.run.failed",
        "companyId": "co",
        "payload": {"errorCode": code, "issueId": issue_id, "agentId": X},
    }


async def test_schedule_only_platform_codes_with_an_issue(monkeypatch):
    monkeypatch.setattr(esc, "CHECK_DELAY_S", 0)
    with patch.object(esc, "escalate", AsyncMock()) as escalate:
        for event in (
            _failed("setup_failed"),
            _failed("process_lost"),
            _failed("timeout"),
            _failed("hermes_gateway_run_failed"),
            _failed("setup_failed", issue_id=None),
            {**_failed("setup_failed"), "eventType": "agent.run.finished"},
        ):
            esc.schedule(event)
        await asyncio.gather(*esc._tasks)
    assert sorted(c.args[3] for c in escalate.await_args_list) == ["process_lost", "setup_failed", "timeout"]
    assert escalate.await_args_list[0].args[:3] == ("co", "iss", X)
