"""`POST /api/v1/tasks/{id}/questions/{qid}/answer` (wave2-Q Task 4 part 2; spec 05 §3.5, §5.1).

The update is faked by running the real `record_answer` Activity (what the workflow's handler does), so the row,
the Effect and the Trigger are the real ones. Adversarial: wrong person, demoted admin, a computer / OAuth / hook
caller, answer after Stop, two presses at once, a replayed press, a bad option, free text stays off the update."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from core.auth import AuthContext
from core.repositories import effects_repo, questions_repo, tasks_repo
from core.services import questions, super_agent_flags
from core.temporal import client as temporal_client
from core.temporal import task_questions as tq
from routers import questions as router_mod

from .conftest import TASK, WS

SAM, ADA, PRIYA = "user_sam", "user_ada", "user_priya"
POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}
MEMBERS = [
    {"user_id": SAM, "role": "org:member"},
    {"user_id": ADA, "role": "org:admin"},
    {"user_id": PRIYA, "role": "org:member"},
]


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(router_mod, "human_display_name", AsyncMock(side_effect=lambda u: u.split("_")[1].title()))
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
    roster = {"members": list(MEMBERS)}

    async def members(org, *, strict=False):
        return roster["members"]

    monkeypatch.setattr(questions.clerk_admin, "list_organization_members", members)
    updates: list[tuple] = []

    async def update_task(ws, task_id, name, ref):
        updates.append((ws, task_id, name, ref))
        return await ActivityEnvironment().run(tq.record_answer, ws, task_id, ref)

    monkeypatch.setattr(temporal_client, "update_task", update_task)
    who = {"user": SAM}
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[router_mod.person_only] = lambda: AuthContext(user_id=who["user"], org_id=WS)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return {"client": client, "who": who, "updates": updates, "roster": roster, "app": app}


async def _question(kind="purchase"):
    await effects_repo.put_effect(
        WS,
        {
            "idemKey": f"{TASK}:1:c1",
            "taskId": TASK,
            "attempt": 1,
            "kind": kind,
            "tool": "request_purchase",
            "operands": {"item": "acme.dev", "amount": "$12"},
            "operandsHash": "h1",
            "state": "proposed",
        },
    )
    [ref] = await ActivityEnvironment().run(tq.open_questions, WS, TASK)
    return ref.qid


def _url(qid):
    return f"/api/v1/tasks/{TASK}/questions/{qid}/answer"


async def _press(world, qid, option="allow_once", cid="c-1", **extra):
    return await world["client"].post(_url(qid), json={"optionId": option, "clientAnswerId": cid, **extra})


async def test_answer_happy_path(world):
    qid = await _question()  # purchase → admins: Ada
    world["who"]["user"] = ADA
    resp = await _press(world, qid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"]["state"] == "answered" and body["question"]["answeredBy"]["id"] == ADA
    assert body["next"]["taskId"] == TASK
    [(_, _, name, ref)] = world["updates"]
    assert name == "answer" and ref == tq.AnswerRef(qid, "allow_once", None, ADA, "c-1")
    assert (await effects_repo.get_effect(WS, f"{TASK}:1:c1"))["state"] == "approved"


async def test_free_text_rides_a_trigger_not_the_update(world):
    qid = await _question()
    world["who"]["user"] = ADA
    resp = await _press(world, qid, "deny", text="Too expensive, use the .com we own")
    assert resp.status_code == 200
    ref = world["updates"][0][3]
    assert ref.trigger_id == f"answer:{qid}:c-1" and "expensive" not in repr(ref)
    trigger = await tasks_repo.get_trigger(WS, ref.trigger_id)
    assert trigger["ref"]["text"] == "Too expensive, use the .com we own"


async def test_answer_wrong_person_403(world):
    qid = await _question()
    world["who"]["user"] = SAM  # the requester; purchases are admins only
    assert (await _press(world, qid)).json()["detail"]["code"] == "not_answerer"
    world["who"]["user"] = PRIYA
    resp = await _press(world, qid)
    assert resp.status_code in (403, 404)  # not even a viewer of a person task: 404
    assert world["updates"] == []


async def test_demoted_admin_403(world):
    qid = await _question()
    world["roster"]["members"] = [{**m, "role": "org:member"} if m["user_id"] == ADA else m for m in MEMBERS]
    world["who"]["user"] = ADA
    resp = await _press(world, qid)
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_answerer"
    assert world["updates"] == []


async def test_answer_after_stop_409(world):
    qid = await _question()
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    world["who"]["user"] = ADA
    resp = await _press(world, qid)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "not_open",
        "state": "cancelled",
        "by": PRIYA,
        "at": resp.json()["detail"]["at"],
        "reason": "stopped",
    }
    assert world["updates"] == []


async def test_answer_after_close_is_refused_and_no_effect_applies(world):
    """N-F1: a closed task whose Question was left open (the end-of-task cancel failed) is never answered: 409
    `task_closed`, no update (no fresh run for a finished task), the purchase Effect stays `proposed`."""
    qid = await _question()
    await tasks_repo.transition_task(WS, TASK, to="resolved", reason="resolved")
    world["who"]["user"] = ADA
    resp = await _press(world, qid)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "task_closed"
    assert world["updates"] == []
    assert (await questions_repo.get_question(WS, TASK, qid))["state"] == "open"
    assert (await effects_repo.get_effect(WS, f"{TASK}:1:c1"))["state"] == "proposed"


@pytest.mark.parametrize("by,reason", [("__system__", "resolved"), ("watch", "watch_gave_up")])
async def test_answer_after_the_task_ended_names_no_person(world, by, reason):
    """The task (not a person) closed it: no system id where the client shows a name, and the reason for its copy."""
    qid = await _question()
    await questions_repo.cancel_open(WS, TASK, by=by, reason=reason)
    world["who"]["user"] = ADA
    detail = (await _press(world, qid)).json()["detail"]
    assert detail["code"] == "not_open" and detail["by"] is None and detail["reason"] == reason


async def test_same_client_answer_id_200_twice(world):
    qid = await _question()
    world["who"]["user"] = ADA
    first = await _press(world, qid)
    again = await _press(world, qid)
    assert first.status_code == again.status_code == 200
    assert len(world["updates"]) == 1
    other = await _press(world, qid, cid="c-2")
    assert other.status_code == 409 and other.json()["detail"]["by"] == ADA


async def test_concurrent_presses_one_winner(world, monkeypatch):
    """Two admins press at once; the loser's update fails and the route answers from the row."""
    world["roster"]["members"] = [*MEMBERS, {"user_id": "user_bo", "role": "org:admin"}]
    await questions_repo.open_question(
        WS,
        {
            "taskId": TASK,
            "qid": "q-two",
            "kind": "approval",
            "subkind": "purchase",
            "title": "buy acme.dev",
            "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
            "askedBy": {"kind": "worker"},
            "answerers": [ADA, "user_bo"],
            "answererRule": "admins",
            "baseAnswerers": [],
        },
    )

    async def as_user(user, cid):
        app = world["app"]
        app.dependency_overrides[router_mod.person_only] = lambda: AuthContext(user_id=user, org_id=WS)
        return await world["client"].post(_url("q-two"), json={"optionId": "allow_once", "clientAnswerId": cid})

    results = await asyncio.gather(as_user(ADA, "c-a"), as_user("user_bo", "c-b"))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (await questions_repo.get_question(WS, TASK, "q-two"))["state"] == "answered"


async def test_invalid_option_400(world):
    qid = await _question()
    world["who"]["user"] = ADA
    resp = await _press(world, qid, "publish")
    assert resp.status_code == 400 and world["updates"] == []


async def test_temporal_down_503(world, monkeypatch):
    qid = await _question()
    world["who"]["user"] = ADA
    monkeypatch.setattr(temporal_client, "update_task", AsyncMock(side_effect=temporal_client.TemporalUnavailable()))
    resp = await _press(world, qid)
    assert resp.status_code == 503 and resp.json()["detail"]["retryable"] is True
    assert (await questions_repo.get_question(WS, TASK, qid))["state"] == "open"


async def test_workflow_refusal_maps_to_task_closed(world, monkeypatch):
    qid = await _question()
    world["who"]["user"] = ADA
    monkeypatch.setattr(
        temporal_client, "update_task", AsyncMock(side_effect=ApplicationError("task_closed", type="ValueError"))
    )
    resp = await _press(world, qid)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "task_closed"


async def test_switch_off_404(world, monkeypatch):
    qid = await _question()
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=False))
    world["who"]["user"] = ADA
    assert (await _press(world, qid)).status_code == 404 and world["updates"] == []


# --- who may call it at all ------------------------------------------------------------------------


@pytest.fixture
def bare():
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_computer_caller_403(bare, monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(router_mod, "get_human_session", spy)
    resp = await bare.post(
        _url("q-1"),
        json={"optionId": "allow_once", "clientAnswerId": "c"},
        headers={"X-Lightbulb-Proof": "proof", "Authorization": "Bearer x"},
    )
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "computer_caller"
    spy.assert_not_awaited()


async def test_hosted_mcp_token_403(bare, monkeypatch):
    """OAuth / hosted-MCP and template tokens are refused by `get_human_session` before anything is read."""
    monkeypatch.setattr(
        router_mod, "get_human_session", AsyncMock(side_effect=HTTPException(403, "human_session_required"))
    )
    resp = await bare.post(
        _url("q-1"), json={"optionId": "allow_once", "clientAnswerId": "c"}, headers={"Authorization": "Bearer at"}
    )
    assert resp.status_code == 403


async def test_hook_token_401(bare):
    resp = await bare.post(
        _url("q-1"),
        json={"optionId": "allow_once", "clientAnswerId": "c"},
        headers={"Authorization": "Bearer hook-tok"},
    )
    assert resp.status_code in (401, 503)  # not a Clerk JWT (503 only when JWKS is unreachable in the sandbox)
    resp = await bare.post(_url("q-1"), json={"optionId": "allow_once", "clientAnswerId": "c"})
    assert resp.status_code == 401
