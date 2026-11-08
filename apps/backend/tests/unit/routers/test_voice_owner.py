"""Owner mode's backend: only a member seated in the DM acts; a spoken line is their message and wakes the Router;
the voice gets the agent lines the DM receives; nothing else (no reads, approvals or work) is the voice's."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from routers import voice_owner

ROOM = "7f1d2c3b-0000-4000-8000-000000000002"
MEMBERS = [{"kind": "human", "id": "user_maya", "role": "owner"}, {"kind": "agent", "id": "a1", "displayName": "Nova"}]
META = {"ownerId": "org_1", "kind": "dm"}


@pytest.fixture
def m():
    with (
        patch.object(voice_owner, "_require_room_token", lambda *a: None),
        patch.object(voice_owner.messaging_repo, "get_room", AsyncMock(return_value=META)),
        patch.object(voice_owner.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(voice_owner.messaging_repo, "member_home", lambda m, o: o),
    ):
        yield


AGENT = "7f1d2c3b-0000-4000-8000-0000000000aa"


async def test_owner_end_creates_yours_item(m):
    """A talk on the orb is kept as a hidden call room: the transcript and a one-line summary."""
    from routers import voice

    body = voice_owner.EndIn(
        ownerId="org_1", roomToken="t", speakerUserId="user_maya", sessionId="job_1", durationSec=42,
        transcript=[{"who": "caller", "text": "Anything I need to handle?", "t": 0.4},
                    {"who": "assistant", "text": "Priya moved to 11:30. I'm drafting the post.", "t": 2.1},
                    {"who": "nobody", "text": "dropped"}],
    )  # fmt: skip
    with patch.object(voice_owner.messaging_repo, "create_room", AsyncMock()) as create:
        out = await voice_owner.end(ROOM, body)
    kw = create.await_args.kwargs
    assert (kw["owner_id"], kw["kind"], kw["room_id"]) == ("org_1", "call", out["roomId"])
    assert kw["extra"]["hidden"] is True and kw["members"][0]["id"] == "user_maya"
    call = kw["extra"]["call"]
    assert call["ownerMode"] is True and call["durationSec"] == 42 and call["status"] == "ended"
    assert call["transcript"] == [{"who": "caller", "text": "Anything I need to handle?", "t": 0.4},
                                  {"who": "assistant", "text": "Priya moved to 11:30. I'm drafting the post.", "t": 2.1}]  # fmt: skip
    assert call["summary"] == "“Anything I need to handle?”" and call["followUps"] == []
    item = voice._item({"roomId": out["roomId"], "kind": "call", "createdAt": "2026-11-07T08:02:00Z", "call": call})
    assert item["type"] == "call" and item["ownerMode"] is True  # the "Yours" chip's test


async def test_a_talk_with_nothing_said_is_not_kept(m):
    body = voice_owner.EndIn(
        ownerId="org_1", roomToken="t", speakerUserId="user_maya", sessionId="job_2", durationSec=3
    )
    with patch.object(voice_owner.messaging_repo, "create_room", AsyncMock()) as create:
        assert await voice_owner.end(ROOM, body) == {"kept": False}
    create.assert_not_awaited()


async def test_only_the_seated_member_can_keep_a_talk(m):
    body = voice_owner.EndIn(ownerId="org_1", roomToken="t", speakerUserId="user_other", sessionId="j", durationSec=1)
    with pytest.raises(HTTPException) as e:
        await voice_owner.end(ROOM, body)
    assert e.value.status_code == 403


async def test_a_retried_end_keeps_the_row_it_made(m):
    body = voice_owner.EndIn(
        ownerId="org_1", roomToken="t", speakerUserId="user_maya", sessionId="job_1", durationSec=5,
        transcript=[{"who": "caller", "text": "Hi", "t": 0.1}],
    )  # fmt: skip
    conflict = AsyncMock(side_effect=voice_owner.messaging_repo.MessagingRepoError("exists"))
    with patch.object(voice_owner.messaging_repo, "create_room", conflict):
        first = await voice_owner.end(ROOM, body)  # get_room (the fixture) finds the row: a replay
        assert first["kept"] is True
        with patch.object(voice_owner.messaging_repo, "get_room", AsyncMock(side_effect=[META, None])):
            with pytest.raises(voice_owner.messaging_repo.MessagingRepoError):
                await voice_owner.end(ROOM, body)  # no row behind the error: it is raised


def _turn(text="What have we shipped in the last week?", n=0, speaker="user_maya"):
    return voice_owner.TurnIn(ownerId="org_1", roomToken="t", speakerUserId=speaker, sessionId="job_1", n=n, text=text)


async def test_a_spoken_line_is_the_members_message_and_wakes_the_router(m):
    from types import SimpleNamespace

    with patch.object(
        voice_owner.messaging, "create_human_message", AsyncMock(return_value=SimpleNamespace(message_id="m1"))
    ) as ch:
        assert await voice_owner.turn(ROOM, _turn()) == {"messageId": "m1"}
        await voice_owner.turn(ROOM, _turn())  # a retry
    first, again = ch.await_args_list
    auth, room, req = first.args
    assert (auth.user_id, auth.org_id, room, req.text) == (
        "user_maya",
        "org_1",
        ROOM,
        "What have we shipped in the last week?",
    )
    assert first.kwargs == {"via": "voice"}  # exactly a typed message: wake defaults on, so admission → Router
    assert again.args[2].client_message_id == req.client_message_id  # the same message, never a double


async def test_a_fragment_is_only_a_message_and_approves_nothing(m):
    """ "Is" (a cut-off line) is stored as the member's message for the Router like any typed text; no route here
    approves, and typed text never answers a card (the one-tap card does)."""
    from types import SimpleNamespace

    with patch.object(
        voice_owner.messaging, "create_human_message", AsyncMock(return_value=SimpleNamespace(message_id="m2"))
    ) as ch:
        await voice_owner.turn(ROOM, _turn("Is"))
    assert ch.await_args.args[2].text == "Is"
    assert not hasattr(voice_owner, "approve") and "voice_approve" not in vars(voice_owner)


async def test_only_the_seated_member_writes_turns(m):
    with pytest.raises(HTTPException) as e:
        await voice_owner.turn(ROOM, _turn(speaker="user_other"))
    assert e.value.status_code == 403


async def test_start_names_the_assistant_its_agent_and_the_first_cursor(m):
    with (
        patch.object(voice_owner, "assistant_name", AsyncMock(return_value="Nova")),
        patch.object(voice_owner, "latest_seq", AsyncMock(return_value=41)) as seq,
    ):
        out = await voice_owner.start(
            ROOM, voice_owner.OwnerIn(ownerId="org_1", roomToken="t", speakerUserId="user_maya")
        )
    assert out == {"assistantName": "Nova", "agentId": "a1", "after": 41}
    seq.assert_awaited_once_with(ROOM)


async def test_events_are_the_dms_agent_lines_for_the_seated_member_only(m):
    lines = {"messages": [{"id": "a1", "text": "Checking GitHub."}], "after": 6}
    with patch.object(voice_owner, "agent_lines", AsyncMock(return_value=lines)) as al:
        body = voice_owner.EventsIn(ownerId="org_1", roomToken="t", speakerUserId="user_maya", after=5)
        assert await voice_owner.events(ROOM, body) == lines
        al.assert_awaited_once_with(ROOM, 5)
        with pytest.raises(HTTPException) as e:
            await voice_owner.events(ROOM, body.model_copy(update={"speakerUserId": "user_other"}))
    assert e.value.status_code == 403


def test_the_voice_has_no_tool_routes():
    paths = {r.path.rsplit("/", 1)[-1] for r in voice_owner.router.routes}
    assert paths == {"start", "turn", "events", "end"}


async def test_only_a_dm_is_owner_mode_and_the_worker_secret_is_required(m):
    import httpx
    from fastapi import FastAPI

    with patch.object(voice_owner.messaging_repo, "get_room", AsyncMock(return_value={**META, "kind": "channel"})):
        with pytest.raises(HTTPException) as e:
            await voice_owner.turn(ROOM, _turn())
    assert e.value.status_code == 404
    app = FastAPI()
    app.include_router(voice_owner.router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    with patch("core.config.settings.HUDDLE_WORKER_SECRET", "s3cret"):  # no worker secret header
        r = await client.post(f"/internal/voice/owner/{ROOM}/turn", json=_turn().model_dump())
    assert r.status_code == 401
