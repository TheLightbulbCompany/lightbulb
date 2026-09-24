"""Every Teams BFF route that takes an entity id is fenced to the caller's workspace company.

Paperclip authorizes by the member's session, and a Paperclip user can hold
memberships in more than one company, so the session alone does not pin an
entity to THIS workspace. A route with an id path param must depend on one of
the fence dependencies below, or be listed in ``ALLOWLIST`` with a reason.
The allowlist must match exactly, so a fixed route has to leave it too.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from main import app
from routers.teams import agents, approvals, issues, router
from routers.teams.deps import TeamsContext

FENCES = {issues._company_issue, agents._company_agent, approvals._company_approval}
try:  # #3150 adds the run fence
    from routers.teams import runs as _runs

    FENCES.add(_runs._company_run)
except AttributeError:
    pass

_INLINE = "fetches the entity itself and 404s a foreign companyId inline (behaviour-tested)"
_RUNS = "run fence: #3150"
_WORK = "goal/project/routine fence: sweep PR 2"
_SWEEP3 = "attachment content / bare work-product / local-session fence: sweep PR 3"

ALLOWLIST = {
    ("GET", "/teams/agents/{agent_id}"): _INLINE,
    ("GET", "/teams/agents/{agent_id}/runs"): _INLINE,
    ("GET", "/teams/routines/{routine_id}/activity"): _INLINE,
    ("GET", "/teams/runs/{run_id}"): _RUNS,
    ("GET", "/teams/heartbeat-runs/{run_id}"): _RUNS,
    ("GET", "/teams/heartbeat-runs/{run_id}/events"): _RUNS,
    ("GET", "/teams/heartbeat-runs/{run_id}/log"): _RUNS,
    ("POST", "/teams/heartbeat-runs/{run_id}/cancel"): _RUNS,
    ("POST", "/teams/heartbeat-runs/{run_id}/watchdog-decisions"): _RUNS,
    ("GET", "/teams/goals/{goal_id}"): _WORK,
    ("PATCH", "/teams/goals/{goal_id}"): _WORK,
    ("DELETE", "/teams/goals/{goal_id}"): _WORK,
    ("GET", "/teams/projects/{project_id}"): _WORK,
    ("PATCH", "/teams/projects/{project_id}"): _WORK,
    ("DELETE", "/teams/projects/{project_id}"): _WORK,
    ("GET", "/teams/routines/{routine_id}"): _WORK,
    ("PATCH", "/teams/routines/{routine_id}"): _WORK,
    ("DELETE", "/teams/routines/{routine_id}"): _WORK,
    ("POST", "/teams/routines/{routine_id}/run"): _WORK,
    ("GET", "/teams/routines/{routine_id}/runs"): _WORK,
    ("GET", "/teams/routines/{routine_id}/revisions"): _WORK,
    ("POST", "/teams/routines/{routine_id}/revisions/{revision_id}/restore"): _WORK,
    ("POST", "/teams/routines/{routine_id}/triggers"): _WORK,
    ("PATCH", "/teams/routines/{routine_id}/triggers/{trigger_id}"): _WORK,
    ("DELETE", "/teams/routines/{routine_id}/triggers/{trigger_id}"): _WORK,
    ("POST", "/teams/routines/{routine_id}/triggers/{trigger_id}/rotate-secret"): _WORK,
    ("GET", "/teams/attachments/{attachment_id}/content"): _SWEEP3,
    ("DELETE", "/teams/work-products/{work_product_id}"): _SWEEP3,
    ("GET", "/teams/local-sessions/{session_id}/work-link"): _SWEEP3,
    ("PATCH", "/teams/local-sessions/{session_id}/work-link"): _SWEEP3,
}


def _calls(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _calls(d)


def _unfenced():
    out = set()
    for r in router.routes:
        if not re.search(r"\{\w+\}", r.path):
            continue
        if FENCES & set(_calls(r.dependant)):
            continue
        out |= {(m, r.path) for m in r.methods}
    return out


def test_every_id_route_is_fenced_or_allowlisted():
    unfenced = _unfenced()
    assert unfenced - ALLOWLIST.keys() == set(), "unfenced id routes: add a fence dependency"
    assert ALLOWLIST.keys() - unfenced == set(), "stale allowlist entries: the route is fenced now, delete them"


# ---- end-to-end: a foreign entity is 404 and nothing else is forwarded ----

_CTX = TeamsContext(
    user_id="u1", org_id="o1", owner_id="o1", company_id="co_abc", paperclip_user_id="p1", session_cookie="cookie"
)


@pytest.fixture
def client():
    async def fake_ctx():
        return _CTX

    app.dependency_overrides[agents._ctx] = fake_ctx
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(agents._ctx, None)


def _foreign_admin(monkeypatch):
    admin = MagicMock()
    admin.get_issue = AsyncMock(return_value={"id": "iss1", "companyId": "co_other"})
    admin.get_approval = AsyncMock(return_value={"id": "apv1", "companyId": "co_other"})
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    return admin


@pytest.mark.real_fences
@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/issues/iss1"),
        ("patch", "/issues/iss1"),
        ("delete", "/issues/iss1"),
        ("get", "/issues/iss1/comments"),
        ("post", "/issues/iss1/comments"),
        ("delete", "/issues/iss1/attachments/att1"),
        ("get", "/issues/iss1/documents/plan"),
        ("post", "/issues/iss1/interactions/int1/accept"),
        ("post", "/issues/iss1/interactions/int1/respond"),
        ("get", "/issues/iss1/feedback-votes"),
        ("post", "/issues/iss1/tree-holds"),
        ("post", "/issues/iss1/tree-holds/h1/release"),
        ("post", "/approvals/apv1/approve"),
        ("post", "/approvals/apv1/reject"),
    ],
)
def test_foreign_entity_is_404_and_never_forwarded(client, monkeypatch, method, url):
    admin = _foreign_admin(monkeypatch)
    r = client.request(method.upper(), f"/api/v1/teams{url}", json={}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 404, r.text
    called = {name for name, *_ in admin.mock_calls} - {"get_issue", "get_approval"}
    assert called == set(), f"forwarded past the fence: {called}"
