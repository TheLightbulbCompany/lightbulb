"""Session chat follows the session (spec §2.7, §5) on real Moto rows."""

import base64
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo
from core.services import local_sessions as service, messaging, session_chat, session_screen
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import fixture, relay, start
from tests.unit.services.test_local_sessions import ALICE, BOB

db = registry_tests.db
transport = relay_tests.transport


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_chat_seats_readers_without_admitting_them_and_follows_sharing_and_end(transport):
    sid = await start(transport)
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    assert {m.id for m in (await session_chat.open_chat(ALICE, sid)).members} == {ALICE.user_id, BOB.user_id}
    # A chat seat is not a control grant: the terminal's input door still refuses Bob.
    keys = session_screen.SessionInput(data=base64.b64encode(b"ls\r").decode(), epoch=0)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.type_into(BOB, sid, keys)
    assert refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    seen = await service.get_session(BOB, sid)
    assert (seen.chat_room_id, seen.chat_unread) == (room, 0)
    # Pausing sharing empties the chat of everyone but its owner, and Bob cannot reopen it.
    await service.set_sharing(ALICE, sid, False)
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None
    assert await messaging_repo.get_member(room, "human", ALICE.user_id) is not None
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, sid)
    # Shared again: Bob is seated again on his next open.
    await service.set_sharing(ALICE, sid, True)
    await session_chat.open_chat(BOB, sid)
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is not None
    # The session ends: its chat is archived and stays readable.
    ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", ended)
    assert (await messaging_repo.get_room(room)).get("archivedAt")
