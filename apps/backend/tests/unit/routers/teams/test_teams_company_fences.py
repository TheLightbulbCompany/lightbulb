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
from routers.teams import agents, approvals, issues, router, runs, work
from routers.teams.deps import TeamsContext

FENCES = {
    issues._company_issue,
    agents._company_agent,
    approvals._company_approval,
    runs._company_run,
    work._company_goal,
    work._company_project,
    work._company_routine,
    work._company_routine_trigger,
}

_INLINE = "fetches the entity itself and 404s a foreign companyId inline (behaviour-tested)"
_SWEEP3 = "attachment content / bare work-product / local-session fence: sweep PR 3"

ALLOWLIST = {
    ("GET", "/teams/agents/{agent_id}"): _INLINE,
    ("GET", "/teams/agents/{agent_id}/runs"): _INLINE,
    ("GET", "/teams/heartbeat-runs/{run_id}"): _INLINE,
    ("GET", "/teams/runs/{run_id}"): _INLINE,
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
    admin.get_goal = AsyncMock(return_value={"id": "g1", "companyId": "co_other"})
    admin.get_project = AsyncMock(return_value={"id": "p1", "companyId": "co_other"})
    admin.get_routine = AsyncMock(return_value={"id": "r1", "companyId": "co_other", "triggers": [{"id": "t1"}]})
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
        ("get", "/goals/g1"),
        ("patch", "/goals/g1"),
        ("delete", "/projects/p1"),
        ("get", "/routines/r1"),
        ("post", "/routines/r1/run"),
        ("patch", "/routines/r1/triggers/t1"),
        ("post", "/routines/r1/triggers/t1/rotate-secret"),
    ],
)
def test_foreign_entity_is_404_and_never_forwarded(client, monkeypatch, method, url):
    admin = _foreign_admin(monkeypatch)
    r = client.request(method.upper(), f"/api/v1/teams{url}", json={}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 404, r.text
    called = {name for name, *_ in admin.mock_calls} - {
        "get_issue",
        "get_approval",
        "get_goal",
        "get_project",
        "get_routine",
    }
    assert called == set(), f"forwarded past the fence: {called}"


@pytest.mark.real_fences
@pytest.mark.parametrize("method,suffix", [("patch", ""), ("delete", ""), ("post", "/rotate-secret")])
def test_trigger_not_on_the_routine_is_404_and_never_forwarded(client, monkeypatch, method, suffix):
    """Upstream's /api/routine-triggers/{id} is not routine-scoped: an own-company
    routine must not carry a trigger id that belongs to another routine."""
    admin = MagicMock()
    admin.get_routine = AsyncMock(return_value={"id": "r1", "companyId": "co_abc", "triggers": [{"id": "t_other"}]})
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    r = client.request(
        method.upper(), f"/api/v1/teams/routines/r1/triggers/t1{suffix}", json={}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 404, r.text
    assert {name for name, *_ in admin.mock_calls} == {"get_routine"}
