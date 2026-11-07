"""Activity HTTP routes: auth, the one action door and what each action maps to,
idempotent repeats, the person-input roster check, and the seen cursor."""

# ruff: noqa: F811 -- fixtures are imported and then requested by name.

import pytest
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from tests.unit.services.test_messaging_activity import (  # noqa: F401
    ANN,
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    USER_C,
    _no_user_lookups,
    _room,
    repo,
    say,
    table,
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


@pytest.mark.asyncio
async def test_routes_require_authentication(app, world):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/messaging/activity")).status_code in (401, 403)
        assert (
            await client.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-01-01T00:00:00Z"})
        ).status_code in (401, 403)
        assert (await client.post("/api/v1/messaging/activity/x/actions/approve", json={})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_serves_the_page_and_validates_limit(http):
    room_id = await _room()
    await say(room_id, ANN, "@bob hi", mentions=[USER_B])
    response = await http.get("/api/v1/messaging/activity", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert {i["kind"] for i in body["items"]} == {"mention"}
    assert body["sources"] == {"paperclip": "ok"} and body["unseenCount"] == 1
    assert (await http.get("/api/v1/messaging/activity", params={"limit": 101})).status_code == 400


@pytest.mark.asyncio
async def test_reply_posts_into_the_thread_through_the_canonical_writer(http):
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
async def test_mark_done_is_idempotent_and_navigate_is_client_side(http):
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
async def test_someone_elses_item_or_a_left_room_is_refused(http, world):
    room_id = await _room()
    mention = await say(room_id, ANN, "@carol", mentions=[USER_C])
    carol_item = f"mention:{mention['messageId']}:{room_id}:1"
    assert (await http.post(f"/api/v1/messaging/activity/{carol_item}/actions/mark_done")).status_code == 404
    mine = await say(room_id, ANN, "@bob", mentions=[USER_B])
    world.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("human", USER_B)})
    gone = await http.post(f"/api/v1/messaging/activity/mention:{mine['messageId']}:{room_id}:2/actions/mark_done")
    assert gone.status_code == 403


@pytest.mark.asyncio
async def test_seen_route_is_monotonic(http):
    first = await http.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-09-01T00:00:00Z"})
    assert first.status_code == 200
    older = await http.post("/api/v1/messaging/activity/seen", json={"upTo": "2026-08-01T00:00:00Z"})
    assert older.json()["seenUpTo"] == first.json()["seenUpTo"]


@pytest.mark.asyncio
async def test_a_client_idempotency_key_lets_two_identical_replies_both_post(http):
    room_id = await _room()
    mention = await say(room_id, ANN, "@bob ok?", mentions=[USER_B])
    url = f"/api/v1/messaging/activity/mention:{mention['messageId']}:{room_id}:1/actions/reply"
    key_1, key_2 = "8f0c3e7a-1b2c-4d5e-8f90-123456789abc", "9f0c3e7a-1b2c-4d5e-8f90-123456789abc"
    for key in (key_1, key_1, key_2):  # a retried tap, then a second deliberate "ok"
        assert (await http.post(url, json={"value": "ok", "idempotencyKey": key})).status_code == 200
    rows = [r for r in await repo.list_thread(room_id, mention["messageId"], 10**6) if r["author"]["id"] == USER_B]
    assert [r["text"] for r in rows] == ["ok", "ok"]


@pytest.mark.asyncio
async def test_a_paperclip_item_id_is_not_found(http):
    """Paperclip approvals, issues and interactions left Activity (Phase 1 task 7)."""
    for item_id in ("paperclip:approval:ap-1", "paperclip:assigned_issue:iss-1", "paperclip:interaction:iss-1:i-1"):
        assert (await http.post(f"/api/v1/messaging/activity/{item_id}/actions/approve", json={})).status_code == 404
