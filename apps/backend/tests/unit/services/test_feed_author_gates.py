"""The feed's agents are authors, never seated (messaging.room_agent): every gate on a room turn's
tools admits a roster agent answering in the feed, and still refuses an agent that is not on the
owner's roster. Every other room keeps requiring a seat (those gates' own tests)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo
from core.services import messaging
from core.services.messaging import ApiProblem

OWNER = "org_feed"
FEED = messaging_repo.feed_room_id(OWNER)
AUTHOR = "10000000-0000-4000-8000-000000000004"  # on the roster, never seated
STRANGER = "30000000-0000-4000-8000-000000000001"  # another owner's agent
USER = "user_A"
RUN = "40000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def feed_world(monkeypatch):
    async def get_member(room_id, kind, member_id):
        return {"kind": "human", "id": member_id, "role": "member"} if kind == "human" else None

    monkeypatch.setattr(
        messaging_repo, "get_room", AsyncMock(return_value={"roomId": FEED, "ownerId": OWNER, "kind": "channel"})
    )
    monkeypatch.setattr(messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging_repo, "has_foreign_members", AsyncMock(return_value=False))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AUTHOR, "name": "Growth"}]))


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "allowed"), [(AUTHOR, True), (STRANGER, False)])
async def test_mac_execution_admits_a_feed_author(monkeypatch, agent, allowed):
    from core.services import clerk_admin, desktop_execution

    run = {"ownerId": OWNER, "runId": RUN, "agentId": agent, "roomId": FEED, "status": "started"}
    run |= {"eventKind": "user_request", "triggerMessageId": "m1"}
    monkeypatch.setattr(messaging_repo, "get_run", AsyncMock(return_value=run))
    monkeypatch.setattr(
        messaging_repo, "get_message_by_id", AsyncMock(return_value={"author": {"kind": "human", "id": USER}})
    )
    monkeypatch.setattr(clerk_admin, "list_organization_members", AsyncMock(return_value=[{"user_id": USER}]))
    call = desktop_execution.authorize_run(OWNER, RUN, agent, "room", active=True)
    if allowed:
        assert (await call)["memberId"] == USER
    else:
        with pytest.raises(desktop_execution.DesktopError):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "allowed"), [(AUTHOR, True), (STRANGER, False)])
async def test_subagents_admit_a_feed_author(agent, allowed):
    from core.services import subagents

    key = f"agent:{agent}:lightbulb:channel:{FEED}"
    if allowed:
        assert await subagents.room_and_agent(OWNER, key) == (FEED, agent)
    else:
        with pytest.raises(ApiProblem):
            await subagents.room_and_agent(OWNER, key)


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "allowed"), [(AUTHOR, True), (STRANGER, False)])
async def test_the_subagent_route_scope_admits_a_feed_author(monkeypatch, agent, allowed):
    from core.auth import AuthContext
    from routers import messaging as router

    monkeypatch.setattr(router.service, "require_room_member", AsyncMock(return_value=({"ownerId": OWNER}, {})))
    call = router._subagent_scope(AuthContext(user_id=USER, org_id=OWNER), FEED, agent)
    if allowed:
        assert await call == OWNER
    else:
        with pytest.raises(ApiProblem):
            await call


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "allowed"), [(AUTHOR, True), (STRANGER, False)])
async def test_local_sessions_admit_a_feed_author(agent, allowed):
    import time

    from core.services import local_sessions
    from core.services.local_session_grants import AgentInvocation

    invocation = AgentInvocation(
        owner_id=OWNER, room_id=FEED, agent_id=agent, expires_at=int(time.time()) + 60, invocation_id="i1"
    )
    if allowed:
        await local_sessions.require_caller(invocation, FEED)
    else:
        with pytest.raises(ApiProblem):
            await local_sessions.require_caller(invocation, FEED)


@pytest.mark.asyncio
@pytest.mark.parametrize(("author", "mentioned"), [(AUTHOR, True), (STRANGER, False)])
async def test_an_activity_reply_to_a_feed_authors_post_mentions_it(monkeypatch, author, mentioned):
    from core.auth import AuthContext
    from core.services import activity

    message_id = str(uuid.uuid4())
    msg = {"messageId": message_id, "author": {"kind": "agent", "id": author}}
    item = SimpleNamespace(id="item-1", state="open")
    monkeypatch.setattr(activity, "_message_item", AsyncMock(return_value=(item, {"sk": "s"}, msg)))
    monkeypatch.setattr(activity, "_require_action", lambda *_: None)
    send = AsyncMock()
    monkeypatch.setattr(messaging, "create_human_message", send)
    await activity._run_message_action(
        AuthContext(user_id=USER, org_id=OWNER),
        "mention",
        [message_id, FEED, "3"],
        "reply",
        activity.ActivityActionRequest(value="what about churn?"),
    )
    request = send.await_args.args[2]
    assert [ref.id for ref in request.mentions] == ([author] if mentioned else [])
    assert str(request.thread_root_message_id) == message_id
