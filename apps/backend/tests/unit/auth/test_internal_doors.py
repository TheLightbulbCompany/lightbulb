"""Every internal door names its tenant: it acts only as the tenant's own person (the tenant IS a person, the Clerk
user id) and refuses anyone else it is handed, writing nothing under either account.

Real DynamoDB (moto) and the product's own writers; the only stubs are the outside world (Photon's lookups and
signature, the wake of an agent). A refusal is judged on stored state: every row naming A or B is the same after."""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.auth import resolve_owner_id
from core.engine import api as engine
from core.engine.api import EngineError
from core.engine.contract import AnswerIn, EventIn, LineIn, OpenIn, Principal, StartIn
from core.repositories import messaging_repo
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import local_session_grants, messaging, onboarding_brief, task_conversation, workspace_provisioning
from routers import messaging as messaging_router
from tests.unit.auth.conftest import ddb, snapshot  # noqa: F401
from tests.unit.routers.test_photon_inbound import CUSTOMER, LINE_ROW, _post, client, seams  # noqa: F401

A, B = "user_door_a", "user_door_b"
URL = "https://acme.example"


@pytest.fixture
def env(ddb, monkeypatch):  # noqa: F811
    """A and B each a provisioned account; the stubs the photon door needs."""
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", AsyncMock(return_value=[]))
    return ddb


async def _accounts() -> str:
    for person in (A, B):
        await workspace_provisioning.provision_org(person, provisioner=person)
    return await task_conversation.front_agent_id(A)


def _held(resource) -> list[str]:
    return snapshot(resource, A, B)


def _open(creator: str, tenant: str = A) -> OpenIn:
    return OpenIn(tenant_id=tenant, client_key="door-dm", kind="dm", creator=Principal(kind="member", id=creator))


async def _line(conversation: str, speaker: str, text: str = "hello") -> LineIn:
    return LineIn(
        tenant_id=A,
        conversation_id=conversation,
        principal=Principal(kind="member", id=speaker),
        text=text,
        idempotency_key=f"door-line-{speaker}",
        respond="none",
    )


def _open_room(kind: str, creator: str) -> OpenIn:
    return OpenIn(
        tenant_id=A,
        conversation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"door-{kind}")),
        client_key=f"door-{kind}",
        kind=kind,
        creator=Principal(kind="member", id=creator),
    )


async def _a_dm(front: str) -> str:
    return (await engine.open_conversation(_open(A))).conversation_id


# --- one entry per door: `refuse` hands it B as the person, `write` hands it A and returns A's stored result ---


async def refuse_engine_open_dm(front, stubs, web, held):
    before = held()
    with pytest.raises(EngineError) as refused:
        await engine.open_conversation(_open(B))
    assert refused.value.status == 403
    assert held() == before


async def write_engine_open_dm(front, stubs, web):
    dm = await engine.open_conversation(_open(A))
    assert (await messaging_repo.get_room(dm.conversation_id))["ownerId"] == A
    assert {m["id"] for m in await messaging_repo.list_members(dm.conversation_id) if m["kind"] == "human"} == {A}


async def refuse_engine_append_line(front, stubs, web, held):
    dm = await _a_dm(front)
    before = held()
    with pytest.raises(EngineError) as refused:
        await engine.append_line(await _line(dm, B))
    assert refused.value.status == 403
    assert held() == before


async def write_engine_append_line(front, stubs, web):
    dm = await _a_dm(front)
    sent = await engine.append_line(await _line(dm, A))
    stored = [
        m for m in await messaging_repo.list_messages_before(dm, 10**19, 100) if m["messageId"] == sent.message_id
    ]
    assert [m["author"]["id"] for m in stored] == [A]
    assert (await messaging_repo.get_room(dm))["ownerId"] == A


def _grant(front: str, room: str, requester: str) -> local_session_grants.AgentInvocation:
    return local_session_grants.AgentInvocation(
        owner_id=A,
        room_id=room,
        agent_id=front,
        expires_at=2**40,
        invocation_id=str(uuid.uuid4()),
        requester_id=requester,
    )


async def refuse_agent_reader(front, stubs, web, held):
    dm = await _a_dm(front)
    # B is even seated in A's room, so only the tenant check can refuse (the old rule accepted any member)
    await messaging_repo.upsert_member(
        room_id=dm, owner_id=A, member={"kind": "human", "id": B, "displayName": "B", "role": "member"}
    )
    before = held()
    with pytest.raises(messaging.ApiProblem) as refused:
        await messaging_router.agent_reader(_grant(front, dm, B))
    assert refused.value.status_code == 403
    assert held() == before


async def write_agent_reader(front, stubs, web):
    dm = await _a_dm(front)
    read = await messaging_router.agent_reader(_grant(front, dm, A))
    assert resolve_owner_id(read.auth) == A and read.auth.user_id == A


async def refuse_agent_dm(front, stubs, web, held):
    before = held()
    with pytest.raises(messaging.ApiProblem) as refused:
        await messaging._agent_dm_room_id(A, B, front)
    assert refused.value.status_code == 404
    assert held() == before


async def write_agent_dm(front, stubs, web):
    room = await messaging._agent_dm_room_id(A, A, front)
    assert (await messaging_repo.get_room(room))["ownerId"] == A


async def _brief(submitted_by: str) -> None:
    await WorkspaceRepo().put_pending_brief(
        A, path="grow", user_input=URL, detail=None, brief_text=URL, submitted_by=submitted_by
    )


async def refuse_front_desk(front, stubs, web, held):
    await _brief(B)
    before = held()
    assert await onboarding_brief.start_front_desk_build_for_line(A) is False
    assert held() == before


async def write_front_desk(front, stubs, web):
    await _brief(A)
    assert await onboarding_brief.start_front_desk_build_for_line(A) is True
    room = await messaging._agent_dm_room_id(A, A, front)
    assert (await messaging_repo.get_room(room))["ownerId"] == A
    authors = {m["author"]["id"] for m in await messaging_repo.list_messages_before(room, 10**19, 100)}
    assert A in authors and B not in authors


def _text_from(member: str, stubs) -> None:
    stubs["get_by_address"].return_value = {**LINE_ROW, "owner_id": A, "member_phones": {CUSTOMER: member}}
    stubs["put_message"].return_value = {"received_key": "2026#m1"}


async def refuse_photon_text(front, stubs, web, held):
    _text_from(B, stubs)
    before = held()
    assert _post(web).json() == {"status": "dropped_foreign_member"}
    assert held() == before
    stubs["put_message"].assert_not_called()
    stubs["deliver_to_agent"].assert_not_called()


async def write_photon_text(front, stubs, web):
    await messaging.ensure_default_room(A, A)
    _text_from(A, stubs)
    assert _post(web).json() == {"status": "member_routed"}
    room = messaging.default_room_id(A)
    stored = await messaging_repo.list_messages_before(room, 10**19, 100)
    assert [(m["author"]["id"], m["via"]) for m in stored if m.get("via") == "text"] == [(A, "text")]
    assert (await messaging_repo.get_room(room))["ownerId"] == A


async def refuse_engine_open_call(front, stubs, web, held):
    before = held()
    with pytest.raises(EngineError) as refused:
        await engine.open_conversation(_open_room("call", B))
    assert refused.value.status == 403
    assert held() == before


async def write_engine_open_call(front, stubs, web):
    room = await engine.open_conversation(_open_room("call", A))
    assert (await messaging_repo.get_room(room.conversation_id))["ownerId"] == A
    assert {m["id"] for m in await messaging_repo.list_members(room.conversation_id) if m["kind"] == "human"} == {A}


async def refuse_engine_open_channel(front, stubs, web, held):
    before = held()
    with pytest.raises(EngineError) as refused:
        await engine.open_conversation(_open_room("channel", B))
    assert refused.value.status == 403
    assert held() == before


async def write_engine_open_channel(front, stubs, web):
    room = await engine.open_conversation(_open_room("channel", A))
    assert (await messaging_repo.get_room(room.conversation_id))["ownerId"] == A
    assert {m["id"] for m in await messaging_repo.list_members(room.conversation_id) if m["kind"] == "human"} == {A}


def _event(speaker: str) -> EventIn:
    return EventIn(
        tenant_id=A,
        trigger_id=f"door-event-{speaker}",
        source="webhook",
        admission="candidate",
        principal=Principal(kind="member", id=speaker),
        text="hello",
    )


async def refuse_engine_submit_event(front, stubs, web, held):
    before = held()
    with pytest.raises(EngineError) as refused:
        await engine.submit_event(_event(B))
    assert refused.value.status == 403
    assert held() == before


async def write_engine_submit_event(front, stubs, web):
    sent = await engine.submit_event(_event(A))
    assert sent.created is True and sent.trigger_id == f"door-event-{A}"


async def refuse_engine_start_task(front, stubs, web, held):
    dm = await _a_dm(front)
    before = held()
    request = StartIn(
        tenant_id=A,
        principal=Principal(kind="member", id=B),
        conversation_id=dm,
        trigger_id="door-start",
        source="voice",
        title="t",
        brief="b",
    )
    with pytest.raises(EngineError) as refused:
        await engine.start_task(request)
    assert refused.value.status == 403
    assert held() == before


async def write_engine_start_task(front, stubs, web):
    dm = await _a_dm(front)
    request = StartIn(
        tenant_id=A,
        principal=Principal(kind="member", id=A),
        conversation_id=dm,
        trigger_id="door-start",
        source="voice",
        title="t",
        brief="b",
    )
    assert (await engine.start_task(request)).created is True


async def refuse_engine_answer(front, stubs, web, held):
    before = held()
    request = AnswerIn(
        tenant_id=A,
        task_id="t1",
        qid="q1",
        principal=Principal(kind="member", id=B),
        option_id="yes",
        client_answer_id="door-answer",
    )
    with pytest.raises(EngineError) as refused:
        await engine.answer(request)
    assert refused.value.status == 403
    assert held() == before


async def write_engine_answer(front, stubs, web):
    request = AnswerIn(
        tenant_id=A,
        task_id="t1",
        qid="q1",
        principal=Principal(kind="member", id=A),
        option_id="yes",
        client_answer_id="door-answer",
    )
    with pytest.raises(EngineError) as unknown:  # A is the tenant's person: it reaches the task lookup
        await engine.answer(request)
    assert unknown.value.status == 404


DOORS = {
    "engine open_conversation (call)": (refuse_engine_open_call, write_engine_open_call),
    "engine open_conversation (channel)": (refuse_engine_open_channel, write_engine_open_channel),
    "engine submit_event": (refuse_engine_submit_event, write_engine_submit_event),
    "engine start_task": (refuse_engine_start_task, write_engine_start_task),
    "engine answer": (refuse_engine_answer, write_engine_answer),
    "engine open_conversation (dm)": (refuse_engine_open_dm, write_engine_open_dm),
    "engine append_line (member)": (refuse_engine_append_line, write_engine_append_line),
    "agent_reader (messaging router)": (refuse_agent_reader, write_agent_reader),
    "messaging._agent_dm_room_id": (refuse_agent_dm, write_agent_dm),
    "onboarding_brief front desk": (refuse_front_desk, write_front_desk),
    "photon inbound member text": (refuse_photon_text, write_photon_text),
}


@pytest.mark.parametrize("door", DOORS)
async def test_internal_door_refuses_foreign_person(env, door, seams, client):  # noqa: F811
    """A's row, B's id as the speaker, member, requester or creator: refused, and no row naming A or B changes."""
    front = await _accounts()
    await DOORS[door][0](front, seams, client, lambda: _held(env))


@pytest.mark.parametrize("door", DOORS)
async def test_internal_door_writes_owner_tenant(env, door, seams, client):  # noqa: F811
    """Handed A, the door writes under A's tenant, authored by A."""
    front = await _accounts()
    await DOORS[door][1](front, seams, client)


async def test_voice_doors_answer_an_engine_refusal_as_their_own_403(monkeypatch):
    """An engine refusal reaches the voice worker as the door's 403, not a 5xx."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from routers import voice_meet, voice_owner

    async def refused(*args, **kwargs):
        raise EngineError("not_tenant_person", 403, {"code": "not_tenant_person"})

    async def owner(*args):
        return "room", A, None

    monkeypatch.setattr(engine, "append_line", refused)
    monkeypatch.setattr(engine, "get_conversation", refused)
    monkeypatch.setattr(voice_owner, "_owner", owner)
    body = SimpleNamespace(ownerId=A, text="hi", sessionId="s", n=1, answers=None, roomToken="t", participantId=None)
    with pytest.raises(HTTPException) as turn:
        await voice_owner.turn("room", body)
    assert turn.value.status_code == 403
    monkeypatch.setattr(voice_meet, "_room_id", lambda r: r)
    monkeypatch.setattr(voice_meet, "_require_room_token", lambda *a: None)
    with pytest.raises(HTTPException) as meeting:
        await voice_meet._meeting("room", body)
    assert meeting.value.status_code == 403 and meeting.value.detail == {"code": "not_tenant_person"}
