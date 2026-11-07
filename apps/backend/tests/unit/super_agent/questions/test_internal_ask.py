"""`POST /api/v1/internal/runtime/{owner}/questions` (wave2-Q Task 7): a Worker's ask becomes one Question row
through the `ask` update; the text rides a Trigger, the update carries ids; a retried ask replays."""

# ruff: noqa: F811 -- fixtures imported by name from the orchestration suites.

from unittest.mock import AsyncMock

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from core.repositories import questions_repo, tasks_repo
from core.services import questions
from core.temporal import task_questions as tq
from routers import runtime_internal as ri

from ..orchestration.test_activities_task import WS, _task, world  # noqa: F401
from ..orchestration.test_runtime_internal_routes import H, _running, _secret, api  # noqa: F401

POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}


@pytest.fixture
def ask_api(api, monkeypatch):
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
    monkeypatch.setattr(
        questions.clerk_admin,
        "list_organization_members",
        AsyncMock(return_value=[{"user_id": "sam", "role": "org:member"}, {"user_id": "ada", "role": "org:admin"}]),
    )
    monkeypatch.setattr(ri, "human_display_name", AsyncMock(side_effect=lambda u: u.title()))
    updates = []

    async def update_task(ws, task_id, name, ref):
        updates.append((name, ref))
        if api.closed:  # what the SDK raises when the `ask` validator refuses with ValueError("task_closed")
            raise WorkflowUpdateFailedError(ApplicationError("task_closed", type="ValueError"))
        return (await ActivityEnvironment().run(tq.open_asked_question, ws, task_id, ref)).qid

    monkeypatch.setattr(ri.temporal, "update_task", update_task)
    api.updates = updates
    api.closed = False
    return api


def _url(ws):
    return f"/api/v1/internal/runtime/{ws}/questions"


async def _body(tid, call="ask-1", **over):
    return {
        "taskId": tid,
        "runId": (await tasks_repo.get_attempt(WS, tid, 1))["runId"],
        "attempt": 1,
        "runSecret": await _secret(tid),
        "toolCallId": call,
        "kind": "question",
        "subkind": "question",
        "title": "Which Stripe account?",
        "text": "Both are connected.",
        "options": [{"id": "o1", "label": "Live"}, {"id": "o2", "label": "Test"}],
        "blocking": True,
    } | over


async def test_task_run_ask_posts_internal_route(ask_api):
    tid = await _task()
    await _running(tid)
    resp = await ask_api.post(_url(WS), json=await _body(tid), headers=H)
    assert resp.status_code == 200, resp.text
    body = await _body(tid)
    qid = questions.qid_for(f"{tid}:{body['runId']}:ask-1")
    assert resp.json() == {"questionId": qid, "state": "open", "waitingLabel": "Waiting on Sam"}
    [(name, ref)] = ask_api.updates
    assert name == "ask" and ref == tq.AskRef(f"ask:{qid}", "question", "question", True)  # ids only
    trigger = await tasks_repo.get_trigger(WS, f"ask:{qid}")
    assert trigger["ref"]["title"] == "Which Stripe account?" and trigger["ref"]["options"][0]["label"] == "Live"
    q = await questions_repo.get_question(WS, tid, qid)
    assert (q["kind"], q["title"], q["answerers"]) == ("question", "Which Stripe account?", ["sam"])


async def test_same_ask_again_replays(ask_api):
    tid = await _task()
    await _running(tid)
    first = await ask_api.post(_url(WS), json=await _body(tid), headers=H)
    again = await ask_api.post(_url(WS), json=await _body(tid), headers=H)
    assert first.json() == again.json() and len(ask_api.updates) == 1


async def test_approval_ask_goes_to_admins(ask_api):
    tid = await _task()
    await _running(tid)
    options = [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}]
    body = await _body(tid, "ask-2", kind="approval", subkind="purchase", options=options)
    resp = await ask_api.post(_url(WS), json=body, headers=H)
    assert resp.status_code == 200 and resp.json()["waitingLabel"] == "Waiting on Ada"


async def test_internal_ask_stale_run_409(ask_api):
    tid = await _task()
    await _running(tid)
    good = await _body(tid)
    assert (await ask_api.post(_url(WS), json={**good, "runSecret": "x"}, headers=H)).status_code == 403
    await tasks_repo.update_attempt(WS, tid, 1, {"status": "ended"}, allowed_status=frozenset({"running"}))
    resp = await ask_api.post(_url(WS), json=good, headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "stale_run"
    qid = questions.qid_for(f"{tid}:{good['runId']}:ask-1")
    assert ask_api.updates == [] and await tasks_repo.get_trigger(WS, f"ask:{qid}") is None


async def test_closed_task_409(ask_api):
    tid = await _task()
    await _running(tid)
    ask_api.closed = True
    resp = await ask_api.post(_url(WS), json=await _body(tid), headers=H)
    assert resp.status_code == 409 and resp.json()["detail"] == "task_closed"


async def test_context_bound_subkinds_are_refused_before_any_write(ask_api):
    tid = await _task()
    await _running(tid)
    for subkind in ("publish", "a2a_hold", "root"):
        body = await _body(tid, f"ask-{subkind}", kind="approval", subkind=subkind)
        assert (await ask_api.post(_url(WS), json=body, headers=H)).status_code == 422
    assert ask_api.updates == []


async def test_too_many_options(ask_api):
    tid = await _task()
    await _running(tid)
    five = [{"id": f"o{i}", "label": str(i)} for i in range(5)]
    assert (await ask_api.post(_url(WS), json=await _body(tid, options=five), headers=H)).status_code == 422
