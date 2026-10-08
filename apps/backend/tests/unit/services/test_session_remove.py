"""The owner removes an ended session from the lists (`removedAt`), on real Moto rows."""

import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.auth import get_current_user, get_human_session
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, messaging, session_chat, session_links, session_resume
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_session_relay import connect, fixture, registration, relay
from tests.unit.services.test_local_sessions import ALICE, BOB, MAC, OWNER, ROOM

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(session_resume, "submissions_enabled", lambda owner: True)


async def end(transport, sid, generation="generation-1", cid="conn-1"):
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": generation, "endedAt": 10005}
    await relay(transport).handle_message(cid, frame)
    row = await repo.get_session(OWNER, sid)
    # A real conversation, and recent: the listings show it.
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    now = int(time.time())
    await repo.put_session({**row, "eventSeq": 3, "endedAt": now, "lastEventAt": now}, row, enrollment)


async def listed(auth) -> list[str]:
    return [s.session_id for s in (await service.list_workspace_sessions(auth, include_ended=True)).sessions]


def app(auth) -> FastAPI:
    from routers import messaging as router

    api = FastAPI()
    api.include_router(router.router, prefix="/api/v1")
    api.add_exception_handler(
        ApiProblem, lambda request, exc: JSONResponse(status_code=exc.status_code, content=exc.body())
    )
    api.dependency_overrides[get_current_user] = api.dependency_overrides[get_human_session] = lambda: auth
    return api


async def http(auth, method: str, path: str):
    async with AsyncClient(transport=ASGITransport(app=app(auth)), base_url="http://test") as client:
        return await client.request(method, f"/api/v1/messaging/sessions{path}")


@pytest.mark.asyncio
async def test_only_its_owner_removes_a_session_and_only_once_it_has_ended(transport, s3):
    sid = await claude_session(transport)
    live = await http(ALICE, "DELETE", f"/{sid}")
    assert live.status_code == 409 and "LOCAL_SESSION_CONFLICT" in live.text
    await end(transport, sid)
    assert (await http(BOB, "DELETE", f"/{sid}")).status_code == 403
    assert await listed(ALICE) == [sid] and await listed(BOB) == [sid]

    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    assert await listed(ALICE) == [] and await listed(BOB) == []
    assert (await service.list_resources(BOB, ROOM, include_ended=True)).sessions == []
    # Gone on every door, for its owner too; the row and its history are kept.
    for auth in (ALICE, BOB):
        assert (await http(auth, "GET", f"/{sid}")).status_code == 404
    with pytest.raises(ApiProblem) as refused:
        await session_resume.request_resume(
            BOB, sid, session_resume.ResumeRequest(resumeId="77777777-7777-4777-8777-777777777777")
        )
    assert refused.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and row["eventSeq"] == 3 and "resumeAllowed" not in row
    # Idempotent; still not Bob's to remove; an unknown id is a 404.
    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    assert (await http(BOB, "DELETE", f"/{sid}")).status_code == 403
    assert (await http(ALICE, "DELETE", "/99999999-9999-4999-8999-999999999999")).status_code == 404


@pytest.mark.asyncio
async def test_removing_a_session_turns_its_link_off_and_empties_its_chat(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    token = link.url.split("#", 1)[1]
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    await session_chat.open_chat(ALICE, sid)
    await end(transport, sid)
    assert (await session_links.resolve(None, token))[0]["sessionId"] == sid
    key = (await messaging_repo.get_room(room))["deliveryKey"]

    await service.remove_session(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert "shareLink" not in row and not row.get("pickupAllowed")
    assert await messaging_repo._get(service._share_pk(service._token_hash(token)), "META") is None
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    # The chat stays archived, and nobody but its owner is left in it or can open it again.
    meta = await messaging_repo.get_room(room)
    assert meta.get("archivedAt") and meta["deliveryKey"] != key
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None
    assert await messaging_repo.get_member(room, "human", ALICE.user_id) is not None
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, sid)
    with pytest.raises(ApiProblem):
        await service.put_link(ALICE, sid, "anyone")


@pytest.mark.asyncio
async def test_the_same_conversation_started_again_is_listed_again(transport, s3):
    sid = await claude_session(transport)
    await end(transport, sid)
    await service.remove_session(ALICE, sid)
    # The same run draining its ended mirror does not bring it back.
    frame = registration(provider="claude", nativeConversationId=pickup_tests.CONVERSATION, providerVersion="2.1.283")
    await relay(transport).handle_message("conn-1", frame)
    assert (await repo.get_session(OWNER, sid)).get("removedAt")
    await end(transport, sid)
    # A new run of it (a resume on the Mac) does.
    connect(transport, cid="conn-2", stamp="1001")
    frame["session"]["generation"] = "generation-2"
    await relay(transport).handle_message("conn-2", frame)
    assert "removedAt" not in await repo.get_session(OWNER, sid)
    assert await listed(BOB) == [sid]


@pytest.mark.asyncio
async def test_remove_all_ended_takes_only_the_callers_own_ended_sessions(transport, s3):
    ended = await claude_session(transport)
    await end(transport, ended)
    live = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="still-running")))[
        "sessionId"
    ]
    assert (await http(ALICE, "DELETE", "")).status_code == 422
    assert (await http(ALICE, "DELETE", "?ended=false")).status_code == 400
    # Bob has none of his own: Alice's stay.
    assert (await http(BOB, "DELETE", "?ended=true")).json() == {"removed": 0}
    assert sorted(await listed(BOB)) == sorted([ended, live])
    assert (await http(ALICE, "DELETE", "?ended=true")).json() == {"removed": 1}
    assert await listed(BOB) == [live]
    assert (await http(ALICE, "DELETE", "?ended=true")).json() == {"removed": 0}
