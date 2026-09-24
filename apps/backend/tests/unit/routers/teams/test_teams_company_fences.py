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
_SESSION = (
    "our own DDB local-session row, caller-scoped by sessions.authorize_session; "
    "the linked issue/project is company-checked by resolve_work"
)

ALLOWLIST = {
    ("GET", "/teams/agents/{agent_id}"): _INLINE,
    ("GET", "/teams/agents/{agent_id}/runs"): _INLINE,
    ("GET", "/teams/heartbeat-runs/{run_id}"): _INLINE,
    ("GET", "/teams/runs/{run_id}"): _INLINE,
    ("GET", "/teams/local-sessions/{session_id}/work-link"): _SESSION,
    ("PATCH", "/teams/local-sessions/{session_id}/work-link"): _SESSION,
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
        ("get", "/issues/iss1/attachments/att1/content"),
        ("delete", "/issues/iss1/work-products/11111111-1111-1111-1111-111111111111"),
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


# ---- own-company issue, but the child entity is not on it: still 404, never forwarded ----

_WP = "11111111-1111-1111-1111-111111111111"


@pytest.mark.real_fences
@pytest.mark.parametrize(
    "method,url,forward",
    [
        ("get", "/issues/iss1/attachments/att1/content", "open_attachment_stream"),
        ("delete", "/issues/iss1/attachments/att1", "delete_attachment"),
        ("delete", f"/issues/iss1/work-products/{_WP}", "delete_work_product"),
    ],
)
def test_child_not_on_own_issue_is_404_and_never_forwarded(client, monkeypatch, method, url, forward):
    admin = MagicMock()
    admin.get_issue = AsyncMock(return_value={"id": "iss1", "companyId": "co_abc"})
    admin.list_issue_attachments = AsyncMock(return_value=[{"id": "att_elsewhere"}])
    admin.list_issue_work_products = AsyncMock(return_value=[{"id": "22222222-2222-2222-2222-222222222222"}])
    setattr(admin, forward, AsyncMock())
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    r = client.request(method.upper(), f"/api/v1/teams{url}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404, r.text
    getattr(admin, forward).assert_not_awaited()


class _Upstream:
    headers = {"content-type": "application/pdf", "content-length": "3"}

    async def aiter_bytes(self):
        yield b"pdf"

    async def aclose(self):
        pass


@pytest.mark.real_fences
def test_own_issue_attachment_content_streams(client, monkeypatch):
    admin = MagicMock()
    admin.get_issue = AsyncMock(return_value={"id": "iss1", "companyId": "co_abc"})
    admin.list_issue_attachments = AsyncMock(return_value=[{"id": "att1"}])
    admin.open_attachment_stream = AsyncMock(return_value=_Upstream())
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    r = client.get("/api/v1/teams/issues/iss1/attachments/att1/content", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200 and r.content == b"pdf"
    admin.open_attachment_stream.assert_awaited_once_with(session_cookie="cookie", attachment_id="att1")


@pytest.mark.real_fences
def test_own_issue_work_product_delete_forwards(client, monkeypatch):
    admin = MagicMock()
    admin.get_issue = AsyncMock(return_value={"id": "iss1", "companyId": "co_abc"})
    admin.list_issue_work_products = AsyncMock(return_value=[{"id": _WP}])
    admin.delete_work_product = AsyncMock(return_value={})
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    r = client.delete(f"/api/v1/teams/issues/iss1/work-products/{_WP}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    admin.delete_work_product.assert_awaited_once_with(session_cookie="cookie", work_product_id=_WP)
