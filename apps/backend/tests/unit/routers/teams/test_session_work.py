"""Optional work links: real session/DynamoDB/HTTP boundaries; synthetic Paperclip."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.auth import get_current_user
from core.repositories import local_sessions_repo as repo
from core.services import local_sessions as sessions
from core.services.messaging import ApiProblem
from core.services.paperclip_admin_client import PaperclipApiError
from routers.teams import router, agents
from tests.unit.services import test_local_sessions as registry_tests, test_local_session_relay as relay_tests
from tests.unit.services.test_local_sessions import registered, ALICE, BOB, OWNER, ROOM, command
from tests.unit.services.test_local_session_relay import start, relay, registration

db = registry_tests.db
transport = relay_tests.transport

COMPANY = "33333333-3333-4333-8333-333333333333"
ISSUE = "44444444-4444-4444-8444-444444444444"
PROJECT = "55555555-5555-4555-8555-555555555555"
WORKSPACE = "66666666-6666-4666-8666-666666666666"


@pytest.fixture
def boundary(db, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: BOB

    @app.exception_handler(ApiProblem)
    async def problem(_request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    company = SimpleNamespace(org_id=OWNER, status="active", company_id=COMPANY, paperclip_user_id="pc-bob")
    monkeypatch.setattr(agents, "_repo", lambda: SimpleNamespace(get=AsyncMock(return_value=company)))
    # Only external sign-in is replaced; per-user resolver/cookie binding remains real.
    import core.services.paperclip_session_cookie as cookie

    monkeypatch.setattr(cookie.settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "synthetic-bind-key")
    minted = []

    async def mint(user):
        minted.append(user)
        return f"__Secure-paperclip-default.session_token=synthetic-{user}"

    monkeypatch.setattr(agents, "get_user_session_cookie_for", mint)
    admin = SimpleNamespace(
        get_issue=AsyncMock(
            return_value={
                "id": ISSUE,
                "companyId": COMPANY,
                "title": "Synthetic investigation",
                "status": "done",
                "description": "NEVER EXPOSE BODY",
            }
        ),
        get_project=AsyncMock(
            return_value={
                "id": PROJECT,
                "companyId": COMPANY,
                "name": "Synthetic project",
                "workspaces": [{"id": WORKSPACE, "name": "Checkout", "cwd": "NEVER EXPOSE CWD"}],
            }
        ),
    )
    monkeypatch.setattr(agents, "_admin", lambda: admin)
    yield app, admin, company, minted
    for user in (ALICE.user_id, BOB.user_id):
        cookie._cold.evict(user)


def path(sid):
    return f"/teams/rooms/{ROOM}/local-sessions/{sid}/work-link"


@pytest.mark.asyncio
async def test_explicit_link_is_per_viewer_minimal_and_lifecycle_independent(boundary):
    app, admin, _, minted = boundary
    sid = await registered()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        empty = await client.get(path(sid))
        assert empty.status_code == 200
        assert empty.json() == {"link": None, "revision": None, "state": "empty"}
        assert minted == []
        linked = await client.patch(
            path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
        )
        assert linked.status_code == 200, linked.text
        data = linked.json()
        assert data["link"] == {"kind": "issue", "companyId": COMPANY, "issueId": ISSUE}
        assert minted == [BOB.user_id]
        assert admin.get_issue.call_args.kwargs["session_cookie"].endswith("synthetic-user_bob")
        row = await repo.get_session(OWNER, sid)
        assert "Synthetic investigation" not in str(row) and "NEVER EXPOSE" not in str(row)
        # Closed issue remains linked, and still admits a normal human follow-up.
        receipt = await sessions.submit_command(BOB, ROOM, sid, command(sessions))
        assert receipt.session_id == sid
        shown = await client.get(path(sid))
        assert shown.json()["title"] == "Synthetic investigation"
        assert "NEVER EXPOSE" not in shown.text
        # Session completion/offline cannot write an issue lifecycle state.
        await registered(status="offline")
        assert (await repo.get_session(OWNER, sid))["workLink"] == data["link"]
        admin.get_issue.side_effect = RuntimeError("PRIVATE UPSTREAM DETAILS")
        hidden = await client.get(path(sid))
        assert hidden.json()["state"] == "unavailable"
        assert "Synthetic investigation" not in hidden.text and "PRIVATE" not in hidden.text
        cleared = await client.patch(path(sid), json={"link": None, "expectedLinkRevision": data["revision"]})
        assert cleared.status_code == 200 and cleared.json()["state"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"id": PROJECT}, {"companyId": PROJECT}])
async def test_issue_must_have_exact_canonical_id_and_company(boundary, change):
    app, admin, _, _ = boundary
    sid = await registered()
    admin.get_issue.return_value.update(change)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        result = await client.patch(
            path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
        )
        assert result.status_code == 404
        assert "Synthetic" not in result.text
    assert "workLink" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_project_workspace_membership_and_link_conflict(boundary):
    app, admin, _, _ = boundary
    sid = await registered()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        body = {"link": {"kind": "workspace", "projectId": PROJECT, "workspaceId": ISSUE}, "expectedLinkRevision": None}
        assert (await client.patch(path(sid), json=body)).status_code == 404
        body["link"]["workspaceId"] = WORKSPACE
        result = await client.patch(path(sid), json=body)
        assert result.status_code == 200
        assert (await client.patch(path(sid), json=body)).status_code == 409
        assert (await client.get(path(sid))).json()["title"] == "Synthetic project · Checkout"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["company", "title"])
async def test_malformed_upstream_identity_or_title_cannot_become_a_link(boundary, malformed):
    app, admin, company, _ = boundary
    sid = await registered()
    if malformed == "company":
        company.company_id = "alias-company"
        admin.get_project.return_value["companyId"] = "alias-company"
    else:
        admin.get_project.return_value["name"] = {"description": "PRIVATE STRUCTURE"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(
            path(sid),
            json={
                "link": {"kind": "workspace", "projectId": PROJECT, "workspaceId": WORKSPACE},
                "expectedLinkRevision": None,
            },
        )
        assert response.status_code == 404
        assert "PRIVATE" not in response.text
    assert "workLink" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_cookie_only_requests_cannot_authorize_session_work(boundary):
    app, admin, _, minted = boundary
    sid = await registered()
    app.dependency_overrides.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get(path(sid), headers={"Cookie": "paperclip-default.session_token=synthetic"})
        assert response.status_code in (401, 403)
    assert minted == []
    admin.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_expired_upstream_session_keeps_refresh_marker_but_not_private_error(boundary):
    app, admin, _, _ = boundary
    sid = await registered()
    seen = []

    @app.exception_handler(PaperclipApiError)
    async def refresh_boundary(request, exc):
        seen.append((request.state.teams_user_id, exc.message, exc.body))
        return JSONResponse(status_code=401, content={"detail": exc.message})

    admin.get_issue.side_effect = PaperclipApiError("PRIVATE upstream auth error", 401, {"secret": "PRIVATE"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(
            path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
        )
        assert response.status_code == 401 and "PRIVATE" not in response.text
    assert seen == [(BOB.user_id, "Linked work unavailable.", {})]
    assert "workLink" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        {"kind": "issue", "issueId": "alias-123"},
        {"kind": "issue", "issueId": ISSUE, "companyId": COMPANY},
        {"kind": "workspace", "projectId": PROJECT, "workspaceId": WORKSPACE, "cwd": "/private"},
    ],
)
async def test_input_cannot_choose_company_or_private_metadata(boundary, link):
    app, admin, _, minted = boundary
    sid = await registered()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(path(sid), json={"link": link, "expectedLinkRevision": None})
        assert response.status_code == 422
    assert minted == []
    admin.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_session_access_is_rechecked_after_upstream(boundary, db):
    app, admin, _, _ = boundary
    sid = await registered()

    async def revoke(**kwargs):
        db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{BOB.user_id}"})
        return {"id": ISSUE, "companyId": COMPANY, "title": "PRIVATE"}

    admin.get_issue.side_effect = revoke
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        result = await client.patch(
            path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
        )
        assert result.status_code == 403
    assert "workLink" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_each_viewer_uses_own_cookie_and_invalid_link_cannot_leak_upstream_errors(boundary):
    app, admin, company, minted = boundary
    sid = await registered()
    app.dependency_overrides[get_current_user] = lambda: ALICE
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        assert (
            await client.patch(
                path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
            )
        ).status_code == 200
        app.dependency_overrides[get_current_user] = lambda: BOB

        async def protected(**kwargs):
            assert kwargs["session_cookie"].endswith("synthetic-user_bob")
            raise PaperclipApiError("PRIVATE TITLE", 403, {"description": "PRIVATE BODY"})

        admin.get_issue.side_effect = protected
        shown = await client.get(path(sid))
        assert shown.status_code == 200 and shown.json()["state"] == "unavailable"
        assert "PRIVATE" not in shown.text
        assert minted == [ALICE.user_id, BOB.user_id]
        company.org_id = "org_stale"
        admin.get_issue.reset_mock()
        assert (await client.get(path(sid))).json()["state"] == "unavailable"
        admin.get_issue.assert_not_called()


@pytest.mark.asyncio
async def test_link_read_rechecks_revision_and_session_access_after_lookup(boundary, db):
    app, admin, _, _ = boundary
    sid = await registered()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        linked = (
            await client.patch(
                path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
            )
        ).json()

        async def unlink(**kwargs):
            cleared = await client.patch(path(sid), json={"link": None, "expectedLinkRevision": linked["revision"]})
            assert cleared.status_code == 200
            return {"id": ISSUE, "companyId": COMPANY, "title": "STALE TITLE"}

        admin.get_issue.side_effect = unlink
        shown = await client.get(path(sid))
        assert shown.status_code == 409 and "STALE" not in shown.text
        admin.get_issue.side_effect = None
        empty = await client.get(path(sid))
        await client.patch(
            path(sid),
            json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": empty.json()["revision"]},
        )

        async def revoke(**kwargs):
            db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{BOB.user_id}"})
            return {"id": ISSUE, "companyId": COMPANY, "title": "REVOKED TITLE"}

        admin.get_issue.side_effect = revoke
        shown = await client.get(path(sid))
        assert shown.status_code == 403 and "REVOKED" not in shown.text


@pytest.mark.asyncio
async def test_real_transaction_fences_membership_removed_after_last_authorization(boundary, db, monkeypatch):
    app, _, _, _ = boundary
    sid = await registered()
    real = repo.put_session

    async def race(row, previous, enrollment, **kwargs):
        db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{BOB.user_id}"})
        return await real(row, previous, enrollment, **kwargs)

    monkeypatch.setattr(repo, "put_session", race)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(
            path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
        )
        assert response.status_code == 403
    assert "workLink" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_first", [True, False])
async def test_actual_relay_registration_cas_preserves_link_and_native_handoff(
    boundary, transport, monkeypatch, registration_first
):
    app, _, _, _ = boundary
    sid = await start(transport)
    native = relay(transport)
    await native.handle_message(
        "conn-1",
        {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": "history",
            "afterSeq": 0,
            "events": [{"kind": "text", "role": "assistant", "text": "Synthetic investigation: parser rejects null."}],
        },
    )
    # Another eligible human reads the exact relayed history, never a summary or room injection.
    assert (await sessions.list_events(BOB, ROOM, sid)).events[
        0
    ].text == "Synthetic investigation: parser rejects null."
    await sessions.submit_command(BOB, ROOM, sid, command(sessions, text="Cover null in the same session."))
    before = await repo.get_session(OWNER, sid)
    real = repo.put_session
    fired = False
    reregister = {
        **registration(generation="generation-2", status="idle", activeTurnId=None),
        "previousGeneration": "generation-1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:

        async def link():
            return await client.patch(
                path(sid), json={"link": {"kind": "issue", "issueId": ISSUE}, "expectedLinkRevision": None}
            )

        async def race(row, previous, enrollment, **kwargs):
            nonlocal fired
            if not fired:
                fired = True
                if registration_first:
                    await native.handle_message("conn-1", reregister)
                else:
                    assert (await link()).status_code == 200
            return await real(row, previous, enrollment, **kwargs)

        monkeypatch.setattr(repo, "put_session", race)
        if registration_first:
            assert (await link()).status_code == 200
        else:
            assert (await native.handle_message("conn-1", reregister))["sessionId"] == sid
    row = await repo.get_session(OWNER, sid)
    assert fired and row["generation"] == "generation-2"
    assert row["eventSeq"] == before["eventSeq"] == 1
    assert row["commandSeq"] == before["commandSeq"] == 1
    assert row["workLink"]["issueId"] == ISSUE
    assert (await sessions.list_events(ALICE, ROOM, sid)).events[
        0
    ].text == "Synthetic investigation: parser rejects null."
