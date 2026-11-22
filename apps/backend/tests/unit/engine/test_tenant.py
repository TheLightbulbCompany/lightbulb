"""Tenant and principal come from the caller (engine-service plan Task 7): a `user_` tenant works through every
operation with no Clerk organisation or membership read, a member principal is trusted as the tenant's member, and
"no person behind a trigger" is the trigger's `actorKind`, never the shape of the tenant id."""

import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from core import tasks_schema as ts
from core.engine import api
from core.engine.contract import ActIn, AnnotateIn, AnswerIn, EventIn, LineIn, OpenIn, Principal, StartIn
from core.repositories import effects_repo, questions_repo, tasks_repo
from core.services import clerk_admin, container_rpc, messaging
from core.temporal import activities as acts
from core.temporal import client as temporal_client

from ..super_agent.orchestration.test_router_turn import ROOM, table, world  # noqa: F401 -- fixtures
from .test_api import FRONT, tables  # noqa: F401 -- fixtures

ME = "user_x"
CALL = "c0000000-0000-4000-8000-0000000000aa"
TASK = "7e8bf6dd-9efb-52b8-ad56-6ed3f06754c1"


@pytest.fixture
def clerk(monkeypatch):
    """Every Clerk organisation and membership read raises. `get_user` (a person's profile, for a display name) is
    best effort and never decides access: it answers nothing, so names fall back to the user id."""
    calls = []

    def refuse(name):
        async def call(*a, **kw):
            calls.append(name)
            raise AssertionError(f"clerk_admin.{name} called on an engine op path")

        return call

    for name, fn in inspect.getmembers(clerk_admin, inspect.iscoroutinefunction):
        if fn.__module__ == clerk_admin.__name__ and name != "get_user":
            monkeypatch.setattr(clerk_admin, name, refuse(name))
    monkeypatch.setattr(clerk_admin, "get_user", AsyncMock(return_value=None))
    return calls


def _task(ws: str, audience: dict) -> dict:
    return {"taskId": TASK, "pk": ts.ws_pk(ws), "identifier": "LB-1", "title": "t", "status": "idle", "version": 3, "updatedAt": "t",
            "triggerId": None, "audience": audience, "ownerUserId": ME}  # fmt: skip


def _question(user: str) -> dict:
    return {"qid": "q-1", "taskId": TASK, "kind": "question", "subkind": "question", "title": "Which?", "text": "",
            "state": "open", "options": [{"id": "a", "label": "A"}], "answerers": [user], "baseAnswerers": [user],
            "answererRule": "requester", "createdAt": "t", "version": 1}  # fmt: skip


def _task_world(monkeypatch, task: dict, user: str):
    """A task and its open Question as stored rows; Temporal accepts every update. `can_view` and `can_answer_now`
    are the real ones."""
    tree = {"task": task, "attempts": [], "questions": [_question(user)]}
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(tasks_repo, "get_task_tree", AsyncMock(return_value=tree))
    monkeypatch.setattr(questions_repo, "get_question", AsyncMock(return_value=_question(user)))
    monkeypatch.setattr(effects_repo, "effects_of_task", AsyncMock(return_value=[]))
    update = AsyncMock()
    monkeypatch.setattr(temporal_client, "update_task", update)
    return update


async def test_user_tenant_through_every_op(tables, clerk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(messaging, "_router_room", AsyncMock(return_value=True))
    monkeypatch.setattr(messaging, "_publish_message_created_soon", lambda item: None)
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", AsyncMock())
    rpc = AsyncMock()
    monkeypatch.setattr(container_rpc, "call", rpc)
    me = Principal(kind="member", id=ME)

    conv = await api.open_conversation(OpenIn(tenant_id=ME, conversation_id=CALL, client_key="k", kind="call",
                                              creator=me))  # fmt: skip
    assert (conv.tenant_id, conv.kind) == (ME, "call")
    assert await api.annotate(AnnotateIn(tenant_id=ME, conversation_id=CALL, set={"call": {"status": "live"}}))
    line = await api.append_line(LineIn(tenant_id=ME, conversation_id=CALL, principal=me, text="Hi",
                                        idempotency_key="turn-1", via="voice"))  # fmt: skip
    assert line.seq == 1
    started = await api.start_task(StartIn(tenant_id=ME, principal=me, conversation_id=CALL, trigger_id="voice:c:1",
                                           source="voice", title="t", brief="b"))  # fmt: skip
    assert (await tasks_repo.get_trigger(ME, started.trigger_id))["actorUserId"] == ME
    event = await api.submit_event(EventIn(tenant_id=ME, trigger_id="webhook:e1", source="webhook",
                                           admission="candidate", principal=me, text="x"))  # fmt: skip
    assert event.created
    assert (await api.read_lines(ME, CALL, None))["after"] == 1

    update = _task_world(monkeypatch, _task(ME, {"kind": "workspace"}), ME)
    assert (await api.get_task(ME, TASK))["taskId"] == TASK
    out = await api.answer(AnswerIn(tenant_id=ME, task_id=TASK, qid="q-1", principal=me, option_id="a",
                                    client_answer_id="c1"))  # fmt: skip
    assert out["question"]["id"] == "q-1"
    await api.act(ActIn(tenant_id=ME, task_id=TASK, action="stop", principal=me))
    assert [c.args[2] for c in update.await_args_list] == ["answer", "stop"]

    api.warm(ME, [FRONT], CALL)
    await asyncio.gather(*api._background)
    assert [c.args[:2] for c in rpc.await_args_list] == [(ME, "health"), (ME, "profile.warm")]
    assert clerk == []


async def test_member_principal_is_trusted_on_an_org_tenant(clerk, monkeypatch):
    """The product's member is the tenant's member: answering and acting on a workspace task read no Clerk
    membership."""
    member = "user_m"
    update = _task_world(monkeypatch, _task("org_1", {"kind": "workspace"}), member)
    who = Principal(kind="member", id=member)
    monkeypatch.setattr(api, "_mark_action", AsyncMock())
    await api.answer(AnswerIn(tenant_id="org_1", task_id=TASK, qid="q-1", principal=who, option_id="a",
                              client_answer_id="c1"))  # fmt: skip
    await api.act(ActIn(tenant_id="org_1", task_id=TASK, action="stop", principal=who))
    assert update.await_count == 2 and clerk == []


_MAIL = {"triggerId": "mail:m1", "source": "mail", "ref": {"text": "hi"}}


def _audience(task: dict) -> tuple:
    a = task["audience"]
    return a["kind"], a.get("userIds") or [], a.get("roomId")


@pytest.mark.parametrize("ws", ["org_1", "user_solo"])
async def test_system_actor_kind_shows_to_room(world, ws):  # noqa: F811
    """Nobody behind it (mail, text): a person-visible ask lands in its room, or the workspace with no room."""
    trig = {**_MAIL, "actorUserId": ws, "actorKind": "system"}
    task = await acts._ensure_task(ws, ts.new_task_id(), trig, "shared", "queued", title="x", visibility="person")
    assert _audience(task) == ("workspace", [], None)
    in_room = {**trig, "triggerId": "mail:m2", "ref": {"text": "hi", "roomId": ROOM}}
    task = await acts._ensure_task(ws, ts.new_task_id(), in_room, "shared", "queued", title="x", visibility="person")
    assert _audience(task) == ("room", [], ROOM)


@pytest.mark.parametrize("ws", ["org_1", "user_solo"])
async def test_person_actor_on_user_tenant_shows_to_person(world, ws):  # noqa: F811
    """A person behind it, whatever the tenant id looks like (on a person's own tenant they are the tenant)."""
    me = ws if ws.startswith("user_") else "user_sam"
    trig = {**_MAIL, "actorUserId": me}
    task = await acts._ensure_task(ws, ts.new_task_id(), trig, "shared", "queued", title="x", visibility="person")
    assert _audience(task) == ("person", [me], None)


@pytest.mark.parametrize("ws,viewer", [("org_1", "user_sam"), ("user_solo", "user_solo")])
async def test_a_tenant_owned_automation_fire_is_visible(world, ws, viewer):  # noqa: F811
    """A fire of an automation the tenant owns (proposed from mail): its person-visible task goes to the workspace,
    which the tenant's people can see (not `userIds=[org_…]`, which nobody can)."""
    from core.services import tasks_access

    fire = {"triggerId": "sched:s1:1", "source": "schedule", "ref": {}, "actorUserId": ws, "actorKind": "system"}
    task = await acts._ensure_task(ws, ts.new_task_id(), fire, "shared", "queued", title="x", visibility="person")
    assert _audience(task) == ("workspace", [], None)
    assert await tasks_access.can_view(task, viewer, member=True)
