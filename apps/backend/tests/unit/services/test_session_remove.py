"""The owner removes an ended session from the lists (`removedAt`), on real Moto rows."""

import time
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
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


async def end(transport, sid, generation="generation-1", cid="conn-1", reason="exited"):
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": generation, "endedAt": 10005, "reason": reason}
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
async def test_only_its_owner_removes_a_session(transport, s3):
    sid = await claude_session(transport)
    assert (await http(BOB, "DELETE", f"/{sid}")).status_code == 403, "a live one is not Bob's to end either"
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


@pytest.mark.asyncio
async def test_a_resume_that_lands_first_keeps_its_link_and_the_remove_answers_409(transport, s3, monkeypatch):
    """The stamp is the commit point: nothing is revoked before it, so a session that started
    again while the DELETE was in flight is live with its link and pick-up intact."""
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    token = link.url.split("#", 1)[1]
    await end(transport, sid)
    transact, resumed = messaging_repo._transact, []

    async def resume_first(items, *args, **kwargs):
        if not resumed and "removedAt" in str(items[0].get("Update", {}).get("UpdateExpression", "")):
            resumed.append(True)
            connect(transport, cid="conn-2", stamp="1001")
            frame = registration(
                provider="claude", nativeConversationId=pickup_tests.CONVERSATION, providerVersion="2.1.283"
            )
            frame["session"]["generation"] = "generation-2"
            await relay(transport).handle_message("conn-2", frame)
        return await transact(items, *args, **kwargs)

    monkeypatch.setattr(messaging_repo, "_transact", resume_first)
    with pytest.raises(ApiProblem) as refused:
        await service.remove_session(ALICE, sid)
    assert resumed and refused.value.error.code == "LOCAL_SESSION_CONFLICT"
    row = await repo.get_session(OWNER, sid)
    assert "removedAt" not in row and row.get("endedAt") is None
    assert row["shareLink"]["tokenHash"] == service._token_hash(token) and row["pickupAllowed"] is True
    assert (await session_links.resolve(None, token))[0]["sessionId"] == sid
    # The cleanup itself never touches a row that is not removed.
    await service.delete_link(ALICE, sid, removed_only=True)
    assert "shareLink" in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_a_cleanup_that_failed_leaves_the_link_dead_and_a_repeat_finishes_it(transport, s3, monkeypatch):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    token = link.url.split("#", 1)[1]
    await end(transport, sid)
    cleanup = service.delete_link
    monkeypatch.setattr(service, "delete_link", AsyncMock(side_effect=RuntimeError("dynamo down")))
    with pytest.raises(RuntimeError):
        await service.remove_session(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and "shareLink" in row
    # Already unreachable: the public door refuses a removed row on its own.
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    assert await listed(BOB) == []
    monkeypatch.setattr(service, "delete_link", cleanup)
    await service.remove_session(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and "shareLink" not in row and not row.get("pickupAllowed")


@pytest.mark.asyncio
async def test_a_run_that_registers_after_the_stamp_keeps_its_chat_seats(transport, s3, monkeypatch):
    """The chat cleanup is fenced like the link cleanup: a new run that cleared `removedAt`
    before it ran keeps everyone who had its chat open."""
    sid = await claude_session(transport)
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    await end(transport, sid)
    cleanup = service.delete_link

    async def resume_then_cleanup(auth, session_id, **kwargs):
        connect(transport, cid="conn-2", stamp="1001")
        frame = registration(
            provider="claude", nativeConversationId=pickup_tests.CONVERSATION, providerVersion="2.1.283"
        )
        frame["session"]["generation"] = "generation-2"
        await relay(transport).handle_message("conn-2", frame)
        return await cleanup(auth, session_id, **kwargs)

    monkeypatch.setattr(service, "delete_link", resume_then_cleanup)
    await service.remove_session(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert "removedAt" not in row and row.get("endedAt") is None
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is not None
    assert not (await messaging_repo.get_room(room)).get("archivedAt")
    assert await listed(BOB) == [sid]


@pytest.mark.asyncio
async def test_removing_a_live_session_ends_it_and_its_end_removes_it(transport, s3):
    """Owner ruling 2026-10-08: remove ends it. One action: End goes to the Mac, and the end is the removal."""
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    room = str((await session_chat.open_chat(BOB, sid)).room_id)

    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    row = await repo.get_session(OWNER, sid)
    commands = await repo.list_commands(OWNER, sid)
    assert [(c["kind"], c["expectedGeneration"], c["actorUserId"]) for c in commands] == [
        ("end", "generation-1", ALICE.user_id)
    ]
    # The row names that End, written in the transaction that admitted it.
    assert row["removeOnEnd"] == commands[0]["requestId"] and "removedAt" not in row and "endedAt" not in row
    await repo.replace_command(commands[0], {**commands[0], "state": "dispatched", "revision": "r2"})
    # Still running until its Mac has stopped it: listed, and readable.
    assert await listed(BOB) == [sid] and (await http(BOB, "GET", f"/{sid}")).status_code == 200

    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and "removeOnEnd" not in row and "resumeAllowed" not in row
    assert "shareLink" not in row and "removalCleanup" not in row
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None
    assert await listed(ALICE) == [] and await listed(BOB) == []
    for auth in (ALICE, BOB):
        assert (await http(auth, "GET", f"/{sid}")).status_code == 404
    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204, "a repeat finishes or confirms it"


@pytest.mark.asyncio
async def test_a_live_session_whose_mac_is_offline_is_not_removed(transport, s3):
    sid = await claude_session(transport)
    row = await repo.get_session(OWNER, sid)
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**row, "onlineUntil": int(time.time()) - 5}, row, enrollment)
    refused = await http(ALICE, "DELETE", f"/{sid}")
    assert refused.status_code >= 400 and "LOCAL_SESSION_OFFLINE" in refused.text and "Mac is offline" in refused.text
    row = await repo.get_session(OWNER, sid)
    assert "removeOnEnd" not in row and "removedAt" not in row
    assert await repo.list_commands(OWNER, sid) == []
    assert await listed(BOB) == [sid]


@pytest.mark.asyncio
async def test_a_session_ended_on_purpose_leaves_the_lists_and_stays_readable_by_id(transport, s3):
    """Owner ruling 2026-10-08. A list-only hide: the panel open on it keeps reading it."""
    sid = await claude_session(transport)
    await end(transport, sid, reason="on-purpose")
    assert (await repo.get_session(OWNER, sid))["endedReason"] == "on-purpose"
    assert await listed(ALICE) == [] and await listed(BOB) == []
    assert (await service.list_resources(BOB, ROOM, include_ended=True)).sessions == []
    members = [{"id": ALICE.user_id, "kind": "human"}, {"id": BOB.user_id, "kind": "human"}]
    assert await service.referenced_sessions(OWNER, members, [sid]) == []
    for auth in (ALICE, BOB):
        seen = await http(auth, "GET", f"/{sid}")
        assert seen.status_code == 200 and seen.json()["status"] == "ended" and seen.json()["endedOnPurpose"] is True
    assert seen.json()["resumable"] is True, "the open panel offers Resume as before"
    # A new run of it (a resume) is listed again.
    connect(transport, cid="conn-2", stamp="1001")
    frame = registration(provider="claude", nativeConversationId=pickup_tests.CONVERSATION, providerVersion="2.1.283")
    frame["session"]["generation"] = "generation-2"
    await relay(transport).handle_message("conn-2", frame)
    assert await listed(BOB) == [sid]
    assert "endedOnPurpose" not in (await http(BOB, "GET", f"/{sid}")).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["exited", "process-exited"])
async def test_a_session_that_ended_any_other_way_stays_listed(transport, s3, reason):
    """The process died, the terminal closed, the Mac shut down: its owner can still resume it."""
    sid = await claude_session(transport)
    await end(transport, sid, reason=reason)
    assert (await repo.get_session(OWNER, sid))["endedReason"] == reason
    assert await listed(BOB) == [sid]
    members = [{"id": ALICE.user_id, "kind": "human"}]
    assert [s["sessionId"] for s in await service.referenced_sessions(OWNER, members, [sid])] == [sid]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "stored", "shown"),
    [
        ("dispatched", "on-purpose", False),
        ("applied", "on-purpose", False),
        ("rejected", "exited", True),
        ("pending", "exited", True),
    ],
)
async def test_the_owners_end_that_reached_the_mac_makes_the_end_on_purpose(transport, s3, state, stored, shown):
    """An older desktop says only "exited"; the relay knows the owner's End was delivered for this run."""
    sid = await claude_session(transport)
    command = service.LocalSessionCommand(
        requestId="88888888-8888-4888-8888-888888888888", expectedGeneration="generation-1", kind="end"
    )
    await service.submit_command(ALICE, None, sid, command)
    sent = await repo.get_command(OWNER, sid, command.request_id)
    await repo.replace_command(sent, {**sent, "state": state, "revision": "r2"})
    await end(transport, sid)
    assert (await repo.get_session(OWNER, sid))["endedReason"] == stored
    assert (await listed(BOB) == [sid]) is shown


async def remove_live(transport, sid, state="dispatched"):
    """DELETE on a running session, with its End in ``state`` on the Mac's side."""
    await service.remove_session(ALICE, sid)
    command = (await repo.list_commands(OWNER, sid))[0]
    await repo.replace_command(command, {**command, "state": state, "revision": "r2"})
    return command


@pytest.mark.asyncio
async def test_an_end_that_is_not_admitted_leaves_no_removal_pending(transport, s3, monkeypatch):
    """Codex on #3927: the mark is written by the transaction that admits the End, so a DELETE
    that failed can never remove the session when that run ends later."""
    sid = await claude_session(transport)
    with monkeypatch.context() as broken:
        broken.setattr(
            repo, "admit_command", AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed."))
        )
        refused = await http(ALICE, "DELETE", f"/{sid}")
    assert refused.status_code == 409
    assert "removeOnEnd" not in await repo.get_session(OWNER, sid)
    await end(transport, sid)
    assert "removedAt" not in await repo.get_session(OWNER, sid) and await listed(BOB) == [sid]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "rejected", "unknown"])
async def test_an_end_the_mac_refused_or_never_got_removes_nothing_later(transport, s3, state):
    """Coordinator ruling on #3927: the removal applies only if its End landed. Otherwise the
    session stays listed and running, and a later end of that run is an ordinary end."""
    sid = await claude_session(transport)
    await remove_live(transport, sid, state)
    assert await listed(BOB) == [sid]
    await end(transport, sid)
    row = await repo.get_session(OWNER, sid)
    assert "removedAt" not in row and "removeOnEnd" not in row
    assert await listed(BOB) == [sid]


@pytest.mark.asyncio
async def test_a_live_session_that_ends_while_remove_is_asked_is_removed_as_ended(transport, s3, monkeypatch):
    sid = await claude_session(transport)
    submit = service.submit_command

    async def ended_first(*args, **kwargs):
        await end(transport, sid)
        return await submit(*args, **kwargs)

    monkeypatch.setattr(service, "submit_command", ended_first)
    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    assert (await repo.get_session(OWNER, sid))["removedAt"] and await listed(BOB) == []


@pytest.mark.asyncio
async def test_a_cleanup_that_failed_at_the_end_is_owed_and_the_owners_next_list_finishes_it(
    transport, s3, monkeypatch
):
    """Codex on #3927: the row is already invisible when the relay's cleanup fails, so nobody can
    repeat the DELETE from the UI. The doors refuse on their own, and the cleanup stays owed."""
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    await remove_live(transport, sid)
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with monkeypatch.context() as broken:
        broken.setattr(service, "delete_link", AsyncMock(side_effect=RuntimeError("dynamo")))
        await relay(transport).handle_message("conn-1", frame)

    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and row["removalCleanup"] is True and row["shareLink"]
    # Nothing was cleaned, and every door already refuses: Bob still has his seat, and cannot use it.
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is not None
    with pytest.raises(ApiProblem) as shut:
        await messaging.require_room_member(OWNER, BOB.user_id, room)
    assert shut.value.error.code == "ROOM_NOT_FOUND"
    await messaging.require_room_member(OWNER, ALICE.user_id, room)
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, sid)
    # Someone else's list does not finish it; its owner's does.
    await listed(BOB)
    assert (await repo.get_session(OWNER, sid))["removalCleanup"] is True
    assert await listed(ALICE) == []
    row = await repo.get_session(OWNER, sid)
    assert "removalCleanup" not in row and "shareLink" not in row and row["removedAt"]
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None
    assert (await messaging_repo.get_room(room)).get("archivedAt")


@pytest.mark.asyncio
async def test_a_repeat_delete_finishes_an_owed_cleanup(transport, s3, monkeypatch):
    sid = await claude_session(transport)
    await service.put_link(ALICE, sid, "anyone")
    await end(transport, sid)
    with monkeypatch.context() as broken:
        broken.setattr(service, "delete_link", AsyncMock(side_effect=RuntimeError("dynamo")))
        with pytest.raises(RuntimeError):
            await service.remove_session(ALICE, sid)
    assert (await repo.get_session(OWNER, sid))["removalCleanup"] is True
    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    row = await repo.get_session(OWNER, sid)
    assert "removalCleanup" not in row and "shareLink" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["pending", "dispatched"])
async def test_a_repeat_remove_while_the_first_end_is_in_flight_sends_no_second_end(transport, s3, first):
    """Codex on #3927: a second DELETE replaced the mark with a newer End that was still waiting
    when the first one stopped the session, and the ended session stayed listed."""
    sid = await claude_session(transport)
    command = await remove_live(transport, sid, first)
    await service.remove_session(ALICE, sid)  # the same answer: no error
    assert [c["requestId"] for c in await repo.list_commands(OWNER, sid)] == [command["requestId"]]
    assert (await repo.get_session(OWNER, sid))["removeOnEnd"] == command["requestId"]
    # The first End lands and the session ends: it is removed.
    sent = await repo.get_command(OWNER, sid, command["requestId"])
    await repo.replace_command(sent, {**sent, "state": "dispatched", "revision": "r3"})
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", frame)
    assert (await repo.get_session(OWNER, sid))["removedAt"] and await listed(BOB) == []


@pytest.mark.asyncio
async def test_a_repeat_remove_after_a_refused_end_sends_a_new_one(transport, s3):
    sid = await claude_session(transport)
    first = await remove_live(transport, sid, "rejected")
    await service.remove_session(ALICE, sid)  # the same answer: no error
    commands = await repo.list_commands(OWNER, sid)
    assert len(commands) == 2
    assert (await repo.get_session(OWNER, sid))["removeOnEnd"] in {c["requestId"] for c in commands} - {
        first["requestId"]
    }


@pytest.mark.asyncio
async def test_two_removes_at_once_admit_one_end_and_keep_one_mark(transport, s3, monkeypatch):
    """Codex on #3927: both DELETEs passed the in-flight precheck, and the second overwrote the
    first's mark. The mark is claimed in the admitting transaction; the loser admits nothing."""
    sid = await claude_session(transport)
    before = await repo.get_session(OWNER, sid)
    await service.remove_session(ALICE, sid)
    first = (await repo.list_commands(OWNER, sid))[0]
    # The second DELETE read the row before the first one's mark landed: it passes the precheck.
    owned, stale = service._owned_session, [before]

    async def read_before_the_first(auth, session_id, verb="share"):
        return stale.pop() if stale else await owned(auth, session_id, verb)

    monkeypatch.setattr(service, "_owned_session", read_before_the_first)
    await service.remove_session(ALICE, sid)  # the same answer: no error
    assert not stale, "it passed the precheck on the stale row and was stopped at admission"
    assert [c["requestId"] for c in await repo.list_commands(OWNER, sid)] == [first["requestId"]]
    assert (await repo.get_session(OWNER, sid))["removeOnEnd"] == first["requestId"]
    # The transaction fences it on its own: a mark is never overwritten, only a dead one replaced.
    row, enrollment = await service.authorize_session(ALICE, None, sid, owner_only=True)
    other = {**first, "requestId": "99999999-9999-4999-8999-999999999999", "commandSeq": row["commandSeq"] + 1}
    other["sk"] = f"COMMAND#{other['requestId']}"
    for replaces in (None, "77777777-7777-4777-8777-777777777777"):
        with pytest.raises(ClientError):
            await repo.admit_command(row, enrollment, other, remove_on_end=True, replaces_mark=replaces)
    assert (await repo.get_session(OWNER, sid))["removeOnEnd"] == first["requestId"]


@pytest.mark.asyncio
async def test_the_end_removes_when_an_end_and_remove_the_row_does_not_name_landed(transport, s3):
    """Whatever the mark says, a landed End-and-remove for this run is its removal, with the cleanup."""
    sid = await claude_session(transport)
    await service.put_link(ALICE, sid, "anyone")
    landed = await remove_live(transport, sid, "dispatched")
    row = await repo.get_session(OWNER, sid)
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**row, "removeOnEnd": "77777777-7777-4777-8777-777777777777"}, row, enrollment)
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert landed["removesOnEnd"] is True
    assert row["removedAt"] and "shareLink" not in row and "removalCleanup" not in row and "removeOnEnd" not in row
    assert await listed(BOB) == []


@pytest.mark.asyncio
async def test_the_owners_plain_end_is_not_a_removal(transport, s3):
    sid = await claude_session(transport)
    command = service.LocalSessionCommand(
        requestId="88888888-8888-4888-8888-888888888888", expectedGeneration="generation-1", kind="end"
    )
    await service.submit_command(ALICE, None, sid, command)
    sent = await repo.get_command(OWNER, sid, command.request_id)
    await repo.replace_command(sent, {**sent, "state": "dispatched", "revision": "r2"})
    await end(transport, sid)
    assert "removedAt" not in await repo.get_session(OWNER, sid)
