"""Teams router tests: stub the company-fence dependencies by default.

Route-logic tests mock the admin client for the call under test only, so a live
fence (which fetches the issue/approval/agent first) would fail every one of
them. The fences are covered on their own: the dependency unit tests and
``test_teams_company_fences``'s structural check. A test that exercises a
fence end to end opts back in with ``@pytest.mark.real_fences``.
"""

import pytest

from main import app
from routers.teams import agents, approvals, cases, issues, work
from routers.teams.agents import SafeId
from routers.teams.approvals import ApprovalId
from routers.teams.issues import IssueId


def _pass_issue(issue_id: IssueId) -> str:
    return issue_id


def _pass_case(case_id: IssueId) -> dict:
    return {"id": case_id, "companyId": "co_abc"}


def _pass_approval(approval_id: ApprovalId) -> str:
    return approval_id


def _pass_agent(agent_id: SafeId) -> str:
    return agent_id


def _pass_goal(goal_id: SafeId) -> str:
    return goal_id


def _pass_project(project_id: SafeId) -> str:
    return project_id


def _pass_routine(routine_id: SafeId) -> str:
    return routine_id


def _pass_trigger(trigger_id: SafeId) -> str:
    return trigger_id


def pytest_configure(config):
    config.addinivalue_line("markers", "real_fences: keep the Teams company-fence dependencies live")


@pytest.fixture(autouse=True)
def _stub_company_fences(request):
    if request.node.get_closest_marker("real_fences"):
        yield
        return
    stubs = {
        issues._company_issue: _pass_issue,
        cases._company_case: _pass_case,
        approvals._company_approval: _pass_approval,
        agents._company_agent: _pass_agent,
        work._company_goal: _pass_goal,
        work._company_project: _pass_project,
        work._company_routine: _pass_routine,
        work._company_routine_trigger: _pass_trigger,
    }
    saved = {k: app.dependency_overrides.get(k) for k in stubs}
    app.dependency_overrides.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                app.dependency_overrides.pop(k, None)
            else:
                app.dependency_overrides[k] = v
