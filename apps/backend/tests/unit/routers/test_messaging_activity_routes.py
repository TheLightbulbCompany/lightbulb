"""Activity HTTP routes: auth, the one action door and what each action maps to,
idempotent repeats, the person-input roster check, and the seen cursor."""

# ruff: noqa: F811 -- fixtures are imported and then requested by name.

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from core.services import activity
from tests.unit.services.test_messaging_activity import (  # noqa: F401
    ANN,
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    USER_C,
    _no_user_lookups,
    _room,
    approval,
    ctx_for,
    INTERACTIONS,
    fake_admin,
    repo,
    say,
    table,
    with_interactions,
    world,
)


@pytest.fixture
async def http(app, world):
    async def bob():
        return AuthContext(user_id=USER_B, org_id=OWNER)

    app.dependency_overrides[get_current_user] = bob
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def paperclip(monkeypatch):
    """Bob's Paperclip context and a fake admin client he reaches it through."""
    admin = fake_admin(approvals=[approval("ap-1", "request_board_approval", {"title": "Ship it"})])
    admin.get_approval.return_value = admin.list_approvals.return_value[0]
    admin.approve_approval.return_value = {"id": "ap-1", "status": "approved"}
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    monkeypatch.setattr(activity, "teams_context", AsyncMock(return_value=ctx_for()))
    return admin


def _decided(admin, status):
    decided = {
        **admin.get_approval.return_value,
        "status": status,
        "decidedByUserId": "pc-bob",
        "decidedAt": "2026-09-24T11:00:00Z",
    }
    admin.get_approval.return_value = decided
    return decided


@pytest.mark.asyncio
async def test_routes_require_authentication(app, world):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/messaging/activity")).status_code in (401, 403)
        assert (
            await client.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-01-01T00:00:00Z"})
        ).status_code in (401, 403)
        assert (await client.post("/api/v1/messaging/activity/x/actions/approve", json={})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_serves_the_page_and_validates_limit(http, paperclip):
    room_id = await _room()
    await say(room_id, ANN, "@bob hi", mentions=[USER_B])
    response = await http.get("/api/v1/messaging/activity", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert {i["kind"] for i in body["items"]} == {"mention", "paperclip"}
    assert body["sources"] == {"paperclip": "ok"} and body["unseenCount"] == 2
    assert (await http.get("/api/v1/messaging/activity", params={"limit": 101})).status_code == 400


@pytest.mark.asyncio
async def test_approve_maps_to_the_bff_and_a_repeat_returns_the_current_state(http, paperclip):
    await _room()

    async def approve(**kwargs):
        _decided(paperclip, "approved")
        return {"id": "ap-1", "status": "approved"}

    paperclip.approve_approval.side_effect = approve
    url = "/api/v1/messaging/activity/paperclip:approval:ap-1/actions/approve"
    first = await http.post(url, json={"note": "go"})
    assert first.status_code == 200
    assert paperclip.approve_approval.await_args.kwargs == {
        "approval_id": "ap-1",
        "note": "go",
        "session_cookie": "cookie",
    }
    item = first.json()
    assert item["state"] == "resolved" and item["actions"] == []
    assert item["resolvedBy"] == {"kind": "human", "id": USER_B, "name": "You"}
    repeat = await http.post(url, json={"note": "go"})
    assert repeat.status_code == 200 and repeat.json()["state"] == "resolved"
    assert paperclip.approve_approval.await_count == 1


@pytest.mark.asyncio
async def test_reject_and_request_revision_map_their_notes(http, paperclip):
    await _room()

    async def reject(**kwargs):
        _decided(paperclip, "rejected")
        return {}

    paperclip.reject_approval.side_effect = reject
    missing = await http.post("/api/v1/messaging/activity/paperclip:approval:ap-1/actions/request_revision", json={})
    assert missing.status_code == 400
    paperclip.request_approval_revision.return_value = {}
    revised = await http.post(
        "/api/v1/messaging/activity/paperclip:approval:ap-1/actions/request_revision", json={"note": "Shorter, please"}
    )
    assert revised.status_code == 200
    assert paperclip.request_approval_revision.await_args.kwargs["decision_note"] == "Shorter, please"
    rejected = await http.post("/api/v1/messaging/activity/paperclip:approval:ap-1/actions/reject", json={})
    assert rejected.status_code == 200 and rejected.json()["state"] == "resolved"
    assert paperclip.reject_approval.await_args.kwargs["reason"] == "Rejected from Activity"


@pytest.mark.asyncio
async def test_approval_of_another_company_is_not_found(http, paperclip):
    paperclip.get_approval.return_value = {**paperclip.get_approval.return_value, "companyId": "co-other"}
    response = await http.post("/api/v1/messaging/activity/paperclip:approval:ap-1/actions/approve", json={})
    assert response.status_code == 404
    paperclip.approve_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_posts_into_the_thread_through_the_canonical_writer(http, paperclip):
    room_id = await _room()
    root = await say(room_id, ANN, "root")
    mention = await say(room_id, ANN, "@bob thoughts?", mentions=[USER_B], thread_root=root["messageId"])
    item_id = f"mention:{mention['messageId']}:{room_id}:{mention['messageSeq']}"
    url = f"/api/v1/messaging/activity/{item_id}/actions/reply"
    assert (await http.post(url, json={})).status_code == 400
    response = await http.post(url, json={"value": "Looks good"})
    assert response.status_code == 200 and response.json()["id"] == item_id
    again = await http.post(url, json={"value": "Looks good"})  # a retried tap posts once
    assert again.status_code == 200
    rows = [r for r in await repo.list_thread(room_id, root["messageId"], 10**6) if r["author"]["id"] == USER_B]
    assert [(r["text"], r["threadRootMessageId"]) for r in rows] == [("Looks good", root["messageId"])]


@pytest.mark.asyncio
async def test_mark_done_is_idempotent_and_navigate_is_client_side(http, paperclip):
    room_id = await _room()
    mention = await say(room_id, ANN, "@bob", mentions=[USER_B])
    item_id = f"mention:{mention['messageId']}:{room_id}:1"
    first = await http.post(f"/api/v1/messaging/activity/{item_id}/actions/mark_done")
    assert first.status_code == 200
    item = first.json()
    assert item["state"] == "done" and item["read"] is True
    assert "mark_done" not in [a["id"] for a in item["actions"]]
    again = await http.post(f"/api/v1/messaging/activity/{item_id}/actions/mark_done")
    assert again.status_code == 200 and again.json()["resolvedAt"] == item["resolvedAt"]
    assert (await http.post(f"/api/v1/messaging/activity/{item_id}/actions/open")).status_code == 400


@pytest.mark.asyncio
async def test_someone_elses_item_or_a_left_room_is_refused(http, paperclip, world):
    room_id = await _room()
    mention = await say(room_id, ANN, "@carol", mentions=[USER_C])
    carol_item = f"mention:{mention['messageId']}:{room_id}:1"
    assert (await http.post(f"/api/v1/messaging/activity/{carol_item}/actions/mark_done")).status_code == 404
    mine = await say(room_id, ANN, "@bob", mentions=[USER_B])
    world.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("human", USER_B)})
    gone = await http.post(f"/api/v1/messaging/activity/mention:{mine['messageId']}:{room_id}:2/actions/mark_done")
    assert gone.status_code == 403


@pytest.mark.asyncio
async def test_reassign_validates_the_person_against_the_workspace_roster(http, paperclip, monkeypatch):
    issue = {
        "id": "iss-1",
        "companyId": "co-1",
        "identifier": "LB-1",
        "title": "Fix",
        "status": "blocked",
        "assigneeUserId": "pc-bob",
    }
    paperclip.get_issue.return_value = issue
    paperclip.patch_issue.return_value = {}
    workspace = SimpleNamespace(member_user_ids=[USER_A, USER_B])
    repo_fake = SimpleNamespace(
        get_workspace=AsyncMock(return_value=workspace),
        get_identity=AsyncMock(return_value=SimpleNamespace(paperclip_user_id="pc-ann")),
    )
    monkeypatch.setattr("routers.teams.agents._repo", lambda: repo_fake)
    url = "/api/v1/messaging/activity/paperclip:assigned_issue:iss-1/actions/reassign"
    for stranger in ("user_outsider", "agent-from-elsewhere", ""):
        assert (await http.post(url, json={"value": stranger})).status_code == 400
    paperclip.patch_issue.assert_not_awaited()
    assert (await http.post(url, json={"value": FRIDAY})).status_code == 200
    # A blocked issue handed to an agent is unblocked in the same PATCH (the Teams BFF rule).
    assert paperclip.patch_issue.await_args.kwargs["body"] == {
        "assigneeAgentId": FRIDAY,
        "assigneeUserId": None,
        "status": "todo",
    }
    assert (await http.post(url, json={"value": USER_A})).status_code == 200
    assert paperclip.patch_issue.await_args.kwargs["body"] == {"assigneeAgentId": None, "assigneeUserId": "pc-ann"}


@pytest.mark.asyncio
async def test_change_status_maps_to_the_issue_patch_and_repeats_are_no_ops(http, paperclip):
    issue = {"id": "iss-1", "companyId": "co-1", "identifier": "LB-1", "title": "Fix", "status": "todo"}
    paperclip.get_issue.return_value = issue
    url = "/api/v1/messaging/activity/paperclip:assigned_issue:iss-1/actions/change_status"
    assert (await http.post(url, json={"value": "exploded"})).status_code == 400
    assert (await http.post(url, json={"value": "todo"})).status_code == 200
    paperclip.patch_issue.assert_not_awaited()

    async def patch(**kwargs):
        paperclip.get_issue.return_value = {**issue, "status": "done"}
        return {}

    paperclip.patch_issue.side_effect = patch
    done = await http.post(url, json={"value": "done"})
    assert done.status_code == 200 and done.json()["state"] == "resolved" and done.json()["actions"] == []
    assert paperclip.patch_issue.await_args.kwargs["body"] == {"status": "done"}


@pytest.mark.asyncio
async def test_seen_route_is_monotonic(http, paperclip):
    first = await http.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-09-01T00:00:00Z"})
    assert first.status_code == 200
    older = await http.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-08-01T00:00:00Z"})
    assert older.json()["seenUpTo"] == first.json()["seenUpTo"]


@pytest.mark.asyncio
async def test_a_client_idempotency_key_lets_two_identical_replies_both_post(http, paperclip):
    room_id = await _room()
    mention = await say(room_id, ANN, "@bob ok?", mentions=[USER_B])
    url = f"/api/v1/messaging/activity/mention:{mention['messageId']}:{room_id}:1/actions/reply"
    key_1, key_2 = "8f0c3e7a-1b2c-4d5e-8f90-123456789abc", "9f0c3e7a-1b2c-4d5e-8f90-123456789abc"
    for key in (key_1, key_1, key_2):  # a retried tap, then a second deliberate "ok"
        assert (await http.post(url, json={"value": "ok", "idempotencyKey": key})).status_code == 200
    rows = [r for r in await repo.list_thread(room_id, mention["messageId"], 10**6) if r["author"]["id"] == USER_B]
    assert [r["text"] for r in rows] == ["ok", "ok"]


@pytest.fixture
def asks(paperclip, monkeypatch):
    """Two pending interactions on Bob's company; the trial gate open."""
    import copy

    rows = copy.deepcopy(INTERACTIONS)
    with_interactions(paperclip, rows)
    monkeypatch.setattr("routers.teams.interactions.require_trial_runway", AsyncMock())

    def resolve(status):
        async def call(issue_id, interaction_id, body, **_):
            row = next(r for r in rows if r["id"] == interaction_id)
            row.update(status=status, resolvedByUserId="pc-bob", resolvedAt="2026-09-24T13:00:00Z")
            return row

        return call

    paperclip.accept_interaction.side_effect = resolve("accepted")
    paperclip.reject_interaction.side_effect = resolve("rejected")
    paperclip.respond_interaction.side_effect = resolve("answered")
    return rows


CONFIRM = "/api/v1/messaging/activity/paperclip:interaction:iss-1:int-confirm/actions"
QUESTION = "/api/v1/messaging/activity/paperclip:interaction:iss-2:int-q/actions"


@pytest.mark.asyncio
async def test_confirm_maps_to_accept_and_a_repeat_returns_the_current_state(http, paperclip, asks):
    first = await http.post(f"{CONFIRM}/accept", json={})
    assert first.status_code == 200
    assert paperclip.accept_interaction.await_args.kwargs == {
        "issue_id": "iss-1",
        "interaction_id": "int-confirm",
        "body": {},
        "session_cookie": "cookie",
    }
    item = first.json()
    assert item["state"] == "resolved" and item["actions"] == [] and item["resolvedBy"]["name"] == "You"
    assert (await http.post(f"{CONFIRM}/accept", json={})).status_code == 200
    assert paperclip.accept_interaction.await_count == 1


@pytest.mark.asyncio
async def test_reject_carries_the_reason(http, paperclip, asks):
    response = await http.post(f"{CONFIRM}/reject", json={"note": "Not this week"})
    assert response.status_code == 200 and response.json()["state"] == "resolved"
    assert paperclip.reject_interaction.await_args.kwargs["body"] == {"reason": "Not this week"}


@pytest.mark.asyncio
async def test_answer_sends_the_chosen_option_and_refuses_an_unlisted_one(http, paperclip, asks):
    bad = await http.post(f"{QUESTION}/answer", json={"value": "o9"})
    assert bad.status_code == 400
    paperclip.respond_interaction.assert_not_awaited()
    ok = await http.post(f"{QUESTION}/answer", json={"value": "o2"})
    assert ok.status_code == 200 and ok.json()["state"] == "resolved"
    assert paperclip.respond_interaction.await_args.kwargs["body"] == {
        "answers": [{"questionId": "q1", "optionIds": ["o2"]}]
    }


@pytest.mark.asyncio
async def test_an_interaction_on_another_companys_issue_is_not_found(http, paperclip, asks):
    paperclip.get_issue.side_effect = lambda issue_id, **_: {"id": issue_id, "companyId": "co-other"}
    response = await http.post(f"{CONFIRM}/accept", json={})
    assert response.status_code == 404
    paperclip.accept_interaction.assert_not_awaited()
