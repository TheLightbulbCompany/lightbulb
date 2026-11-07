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
async def test_a_resume_that_lands_first_keeps_its_link_and_the_remove_answers_409(transport, s3, monkeypatch):
    """The stamp is the commit point: nothing is revoked before it, so a session that started
    again while the DELETE was in flight is live with its link and pick-up intact."""
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    await pickup_tests.pickup.set_pickup(ALICE, sid, True)
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
    # The End itself says it is a removal; the row carries nothing until the run ends.
    assert commands[0]["removesOnEnd"] is True and "removedAt" not in row and "endedAt" not in row
    await repo.replace_command(commands[0], {**commands[0], "state": "dispatched", "revision": "r2"})
    # Still running until its Mac has stopped it: listed, and readable.
    assert await listed(BOB) == [sid] and (await http(BOB, "GET", f"/{sid}")).status_code == 200

    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and "resumeAllowed" not in row and "shareLink" not in row
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
    assert "removedAt" not in row
    assert await repo.list_commands(OWNER, sid) == []
    assert await listed(BOB) == [sid]


@pytest.mark.asyncio
async def test_a_session_ended_on_purpose_stays_listed_until_removed(transport, s3):
    """Owner ruling 2026-10-27 (superseding 2026-10-08's hide): End leaves the row in Recent,
    marked as ended on purpose, until its owner removes it."""
    sid = await claude_session(transport)
    await end(transport, sid, reason="on-purpose")
    assert (await repo.get_session(OWNER, sid))["endedReason"] == "on-purpose"
    assert await listed(ALICE) == [sid] and await listed(BOB) == [sid]
    members = [{"id": ALICE.user_id, "kind": "human"}, {"id": BOB.user_id, "kind": "human"}]
    assert [s["sessionId"] for s in await service.referenced_sessions(OWNER, members, [sid])] == [sid]
    for auth in (ALICE, BOB):
        seen = await http(auth, "GET", f"/{sid}")
        assert seen.status_code == 200 and seen.json()["status"] == "ended" and seen.json()["endedOnPurpose"] is True
    assert seen.json()["resumable"] is True


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
        ("dispatched", "on-purpose", True),
        ("applied", "on-purpose", True),
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
@pytest.mark.parametrize("state", ["pending", "rejected", "unknown"])
async def test_an_end_the_mac_refused_or_never_got_removes_nothing_later(transport, s3, state):
    """Coordinator ruling on #3927: the removal applies only if its End landed. Otherwise the
    session stays listed and running, and a later end of that run is an ordinary end."""
    sid = await claude_session(transport)
    await remove_live(transport, sid, state)
    assert await listed(BOB) == [sid]
    await end(transport, sid)
    assert "removedAt" not in await repo.get_session(OWNER, sid)
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
async def test_a_cleanup_that_failed_at_the_end_leaves_every_door_shut(transport, s3, monkeypatch):
    """Codex on #3927: the row is already invisible when the relay's cleanup fails. Nothing was
    cleaned, and every door refuses the removed row on its own, posts into its chat included."""
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    await remove_live(transport, sid)
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with monkeypatch.context() as broken:
        broken.setattr(service, "delete_link", AsyncMock(side_effect=RuntimeError("dynamo")))
        await relay(transport).handle_message("conn-1", frame)

    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and row["shareLink"]
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is not None
    assert not (await messaging_repo.get_room(room)).get("archivedAt")
    with pytest.raises(ApiProblem) as shut:
        await messaging.require_room_member(OWNER, BOB.user_id, room)
    assert shut.value.error.code == "ROOM_NOT_FOUND"
    await messaging.require_room_member(OWNER, ALICE.user_id, room)
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, sid)
    # The one writer every post goes through, whoever the author is (an agent has no member door).
    with pytest.raises(messaging_repo.RoomNotFound):
        await messaging_repo.create_message(
            room_id=room,
            owner_id=OWNER,
            idemp_pk=messaging_repo.room_pk(room),
            idemp_sk="IDEMP#late",
            req_hash="h",
            author={"kind": "system", "id": "system", "displayName": "Lightbulb"},
            text="late reply",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
        )
    assert int((await messaging_repo.get_room(room))["lastSeq"]) == 0
    # A repeat DELETE by id still finishes it.
    assert (await http(ALICE, "DELETE", f"/{sid}")).status_code == 204
    row = await repo.get_session(OWNER, sid)
    assert row["removedAt"] and "shareLink" not in row
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None


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


@pytest.mark.asyncio
async def test_a_session_posts_into_its_own_chat_while_live_and_not_once_removed(transport, s3):
    """The session's own post already checks its row, and a transaction names an item once: the
    removed fence rides that check instead of adding a second one (a 500 from DynamoDB)."""
    sid = await claude_session(transport)
    room = str((await session_chat.open_chat(ALICE, sid)).room_id)

    async def post(key: str):
        return await messaging_repo.create_message(
            room_id=room,
            owner_id=OWNER,
            idemp_pk=messaging_repo.room_pk(room),
            idemp_sk=f"IDEMP#{key}",
            req_hash=key,
            author={"kind": "system", "id": "claude", "displayName": "Claude"},
            text="from the session",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
            acting_human_id=ALICE.user_id,
            extra_conditions=[repo.native_source_condition(await repo.get_session(OWNER, sid))],
        )

    assert (await post("live"))[1] is False
    row = await repo.get_session(OWNER, sid)
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**row, "removedAt": 1}, row, enrollment)
    with pytest.raises(messaging_repo.RoomNotFound):
        await post("removed")
