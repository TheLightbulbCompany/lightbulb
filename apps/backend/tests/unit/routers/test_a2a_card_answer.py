"""A holder answers a hold by pressing Approve/Deny on the notification card in their DM."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import a2a_repo
from core.services.messaging import ApiProblem
from routers import messaging as router
from tests.unit.routers.test_a2a_hold import AGENT, ORG, OWNER, REQUESTER, RULES, get, send


@pytest.fixture
def card_env(monkeypatch, app, a2a_env):
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}])
    )
    posted: dict[str, object] = {}  # holder or event id -> the agent message request

    def _agent(owner_id, req):
        mid = str(uuid.uuid4())
        posted[req.event_id] = SimpleNamespace(id=mid, req=req)
        return SimpleNamespace(message_id=mid)

    agent = AsyncMock(side_effect=_agent)
    monkeypatch.setattr("core.services.messaging.create_agent_message", agent)
    dispatch = a2a_env.send.side_effect

    def _human(*a, **k):
        if "wake" in k:  # the web post of the press: stop after recording the call
            raise ApiProblem("ROOM_NOT_FOUND", "stop here")
        return dispatch(*a, **k)

    human = AsyncMock(side_effect=_human)
    monkeypatch.setattr("core.services.messaging.create_human_message", human)
    asyncio.run(a2a_repo.put_rules(ORG, RULES))
    yield SimpleNamespace(**vars(a2a_env), agent=agent, human=human, posted=posted)
    app.dependency_overrides.pop(router.person_or_computer, None)


def notification(env, task_id, holder):
    return env.posted[f"a2a:{task_id}:hold:{holder}"]


def press(app, client, user, reply_to, text, room):
    async def _caller():
        return AuthContext(user_id=user, org_id=ORG, org_role="org:member"), None

    app.dependency_overrides[router.person_or_computer] = _caller
    body = {
        "clientMessageId": str(uuid.uuid4()),
        "text": text,
        "mentions": [{"kind": "agent", "id": AGENT}],
        "replyToMessageId": reply_to,
        "threadRootMessageId": reply_to,
    }
    client.post(f"/api/v1/messaging/rooms/{room}/messages", json=body)


def web_wake(env):
    [call] = [c for c in env.human.await_args_list if "wake" in c.kwargs]
    return call.kwargs["wake"]


def state(client, task_id):
    return get(client, task_id)["status"]["state"]


def test_notification_text_carries_the_openui_fence_and_index_row(client, card_env):
    task_id = send(client)["id"]
    n = notification(card_env, task_id, OWNER)
    assert "```openui-lang\n" in n.req.text and 'Button("Approve", "primary")' in n.req.text
    assert 'root = Stack([b])\nb = Buttons([Button("Approve", "primary"), Button("Deny", "secondary")])' in n.req.text
    assert asyncio.run(a2a_repo.get_task_by_notification(ORG, n.id)) == (task_id, OWNER, card_env.room)
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    assert row["notificationMessageIds"][OWNER] == n.id


def test_holder_pressing_approve_decides_and_stores_quietly(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, OWNER, notification(card_env, task_id, OWNER).id, " Approve ", card_env.room)
    assert state(client, task_id) == "TASK_STATE_WORKING"
    assert web_wake(card_env) is False
    assert asyncio.run(a2a_repo.get_task(ORG, task_id))["decisionReason"] == "in-app"


def test_holder_pressing_deny_rejects(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, OWNER, notification(card_env, task_id, OWNER).id, "Deny", card_env.room)
    assert state(client, task_id) == "TASK_STATE_REJECTED"
    assert web_wake(card_env) is False


def test_requester_pressing_approve_is_refused_and_agent_posts_the_reason(client, app, card_env):
    task_id = send(client)["id"]
    # A requester who holds is allowed outright by the rules, so make them a holder of this hold after
    # the fact (as a holders change would) and give them a card of their own.
    card = str(uuid.uuid4())
    asyncio.run(a2a_repo.update_task(ORG, task_id, state="held", extra={"holders": [OWNER, REQUESTER]}))
    asyncio.run(a2a_repo.put_notification(ORG, card, task_id, REQUESTER, card_env.room))
    press(app, client, REQUESTER, card, "Approve", card_env.room)
    failed = card_env.posted[f"a2a:{task_id}:answer-failed:{REQUESTER}"].req
    assert failed.text.startswith("could not record your answer: self")
    assert str(failed.agent_id) == AGENT and str(failed.room_id) == card_env.room
    assert state(client, task_id) == "TASK_STATE_AUTH_REQUIRED"
    assert web_wake(card_env) is False


def test_non_holder_press_is_ignored_as_a_normal_message(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, "user_test_789", notification(card_env, task_id, OWNER).id, "Approve", card_env.room)
    assert web_wake(card_env) is True
    assert state(client, task_id) == "TASK_STATE_AUTH_REQUIRED"


def test_reply_to_a_non_notification_is_a_normal_message(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, OWNER, str(uuid.uuid4()), "Approve", card_env.room)
    assert web_wake(card_env) is True
    assert state(client, task_id) == "TASK_STATE_AUTH_REQUIRED"


def test_other_text_in_reply_to_notification_is_a_normal_message(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, OWNER, notification(card_env, task_id, OWNER).id, "ok thanks", card_env.room)
    assert web_wake(card_env) is True
    assert state(client, task_id) == "TASK_STATE_AUTH_REQUIRED"


def test_press_from_another_room_is_a_normal_message(client, app, card_env):
    task_id = send(client)["id"]
    press(app, client, OWNER, notification(card_env, task_id, OWNER).id, "Approve", str(uuid.uuid4()))
    assert web_wake(card_env) is True
    assert state(client, task_id) == "TASK_STATE_AUTH_REQUIRED"
