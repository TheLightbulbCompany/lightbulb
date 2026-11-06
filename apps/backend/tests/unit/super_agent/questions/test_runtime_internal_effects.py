"""The one effects route (wave2-Q Task 5; contracts-v2 §8): `proceed | replay | refuse | needs_approval | denied`,
plus `…/effects/{idemKey}/done` and the `waiting_approval` terminal run event. Review focus 3: a Worker retrying a
denied call gets `denied` at once, no second row; an approval is used once; a race has one winner."""

# ruff: noqa: F811 -- fixtures imported by name from the orchestration suites.

import asyncio
import hashlib

import pytest

from core.repositories import effects_repo, tasks_repo
from core.services import questions, runtime_runs
from core.temporal.types import WorkerTurnOut
from routers import runtime_internal as ri

from ..orchestration.test_activities_task import WS, _task, world  # noqa: F401
from ..orchestration.test_runtime_internal_routes import H, _running, _secret, api  # noqa: F401

OPERANDS = {"agentId": "a-1", "to": "acme@example.com", "subject": "Q3 invoice", "body": "Attached."}
HASH = ri.operands_hash("mail.send", OPERANDS)


@pytest.fixture(autouse=True)
def _no_rules(monkeypatch):
    """No stored Rules document and the default mode (the messaging and billing tables are not part of this suite):
    every mail here goes to an outsider, so Auto asks, as these tests expect."""
    from unittest.mock import AsyncMock

    from core.repositories import a2a_repo, billing_repo

    monkeypatch.setattr(a2a_repo, "get_rules", AsyncMock(return_value=None))
    monkeypatch.setattr(billing_repo, "get_by_owner_id", AsyncMock(return_value=None))


def _url(ws):
    return f"/api/v1/internal/runtime/{ws}/effects"


async def _body(tid, call="c1", attempt=1, **over):
    return {
        "taskId": tid,
        "attempt": attempt,
        "runId": (await tasks_repo.get_attempt(WS, tid, attempt))["runId"],
        "runSecret": await _secret(tid, attempt),
        "toolCallId": call,
        "kind": "mail.send",
        "tool": "terminal",
        "operands": OPERANDS,
        "operandsHash": HASH,
        "executorOk": True,
        "idemKey": f"{tid}:{attempt}:{call}",
    } | over


def _row(body, **over):
    """The Effect row the route would write for this request body."""
    keep = {k: v for k, v in body.items() if k not in ("runId", "runSecret", "toolCallId")}
    return {**keep, "state": "proposed", **over}


async def _started(api):
    tid = await _task()
    await _running(tid)
    return tid


async def test_needs_approval_writes_proposed_with_draft(api):
    tid = await _started(api)
    resp = await api.post(_url(WS), json=await _body(tid), headers=H)
    assert resp.status_code == 200, resp.text
    qid = questions.qid_for(f"{tid}:1:c1")
    assert resp.json() == {"decision": "needs_approval", "questionId": qid}
    effect = await effects_repo.get_effect(WS, f"{tid}:1:c1")
    assert (effect["state"], effect["questionId"], effect["kind"]) == ("proposed", qid, "mail.send")
    assert effect["operands"]["subject"] == "Q3 invoice" and effect["executorOk"] is True
    # The draft the task card shows as Ready for review with Send (audit item 11).
    assert effect["output"] == {"kind": "mail", "title": "Send 'Q3 invoice' to acme@example.com", "ref": {}}
    # The Worker asks again in the same turn: the same open question, one row.
    again = await api.post(_url(WS), json=await _body(tid, "c2"), headers=H)
    assert again.json() == {"decision": "needs_approval", "questionId": qid}
    assert len(await effects_repo.effects_for(WS, tid, HASH)) == 2  # c2's row shares the qid; no new question


async def test_a_reopened_task_asks_again_after_close_cancelled_the_question(api, monkeypatch):
    """Dev walk 7 (W7-3): the closed task's Question is cancelled; the same call after Reopen gets a new qid (a new
    Question the workflow opens), never the cancelled one."""
    from core.repositories import questions_repo

    tid = await _started(api)
    first = (await api.post(_url(WS), json=await _body(tid), headers=H)).json()["questionId"]
    monkeypatch.setattr(questions_repo, "list_for_task", _async([{"qid": first, "state": "cancelled", "taskId": tid}]))
    again = await api.post(_url(WS), json=await _body(tid, "c2"), headers=H)
    assert again.json() == {"decision": "needs_approval", "questionId": questions.qid_for(f"{tid}:1:c2")}
    assert (await effects_repo.get_effect(WS, f"{tid}:1:c2"))["questionId"] == questions.qid_for(f"{tid}:1:c2")


def _async(value):
    async def f(*_a, **_k):
        return value

    return f


async def test_same_operands_after_deny_returns_denied(api):
    tid = await _started(api)
    await api.post(_url(WS), json=await _body(tid), headers=H)
    await effects_repo.set_effect_state(WS, f"{tid}:1:c1", to="denied", fields={"questionId": "q-d"})
    resp = await api.post(_url(WS), json=await _body(tid, "c2"), headers=H)
    assert resp.json() == {"decision": "denied", "questionId": "q-d"}
    assert await effects_repo.get_effect(WS, f"{tid}:1:c2") is None  # no second card, no new row


async def test_approved_once_then_asks_again(api, monkeypatch):
    tid = await _started(api)
    await api.post(_url(WS), json=await _body(tid), headers=H)
    await effects_repo.set_effect_state(WS, f"{tid}:1:c1", to="approved", fields={"questionId": "q-a"})
    first = await api.post(_url(WS), json=await _body(tid, "c2"), headers=H)
    assert first.json() == {"decision": "proceed"}
    assert (await effects_repo.get_effect(WS, f"{tid}:1:c2"))["state"] == "intent"
    # While c2 is in flight, a third identical call is refused (its outcome is unknown).
    third = await api.post(_url(WS), json=await _body(tid, "c3"), headers=H)
    assert third.json()["decision"] == "refuse" and "attempt 1" in third.json()["message"]
    done = await api.post(
        f"{_url(WS)}/{tid}:1:c2/done",
        json={
            "disposition": "done",
            "resultRef": "mail:sent",
            "runSecret": await _secret(tid),
            "runId": (await _body(tid))["runId"],
        },
        headers=H,
    )
    assert done.status_code == 200 and done.json()["state"] == "done"
    # Right after it landed, the same mail again replays it (gate run 9: never a duplicate send) ...
    fourth = await api.post(_url(WS), json=await _body(tid, "c4"), headers=H)
    assert fourth.json()["decision"] == "replay"
    assert (
        fourth.json()["result"].startswith("Already done in this task at ")
        and "not sent again" in fourth.json()["result"]
    )
    # ... and past the window it is a second send: a fresh approval, the old one is spent.
    monkeypatch.setattr(ri, "_recent_since", lambda: "9999")
    fifth = await api.post(_url(WS), json=await _body(tid, "c5"), headers=H)
    assert fifth.json()["decision"] == "needs_approval"


async def test_replay_same_idem_key_done(api):
    tid = await _started(api)
    await effects_repo.put_effect(WS, _row(await _body(tid), state="intent"))
    await effects_repo.mark_effect_done(WS, f"{tid}:1:c1", disposition="done", result_ref="mail:sent")
    resp = await api.post(_url(WS), json=await _body(tid), headers=H)
    assert resp.json() == {"decision": "replay", "result": "mail:sent"}


async def test_conditional_write_race_one_winner(api):
    tid = await _started(api)
    await api.post(_url(WS), json=await _body(tid), headers=H)
    await effects_repo.set_effect_state(WS, f"{tid}:1:c1", to="approved", fields={"questionId": "q-a"})
    bodies = [await _body(tid, "c2"), await _body(tid, "c3")]
    results = await asyncio.gather(*(api.post(_url(WS), json=b, headers=H) for b in bodies))
    decisions = sorted(r.json()["decision"] for r in results)
    assert decisions.count("proceed") == 1 and decisions[0] != decisions[1]
    rows = [e for e in await effects_repo.effects_for(WS, tid, HASH) if e.get("state") == "intent"]
    assert len(rows) == 1


async def test_stale_run_409(api):
    tid = await _started(api)
    good = await _body(tid)
    wrong_run = await api.post(_url(WS), json={**good, "runId": "00000000-0000-4000-8000-000000000000"}, headers=H)
    assert wrong_run.status_code == 403
    wrong_secret = await api.post(_url(WS), json={**good, "runSecret": "nope"}, headers=H)
    assert wrong_secret.status_code == 403
    await tasks_repo.update_attempt(WS, tid, 1, {"status": "ended"}, allowed_status=frozenset({"running"}))
    stale = await api.post(_url(WS), json=good, headers=H)
    assert stale.status_code == 409 and stale.json()["detail"] == "stale_run"
    assert await effects_repo.get_effect(WS, f"{tid}:1:c1") is None


async def test_bad_idem_key_and_big_operands(api):
    tid = await _started(api)
    bad = await api.post(_url(WS), json=await _body(tid, idemKey=f"{tid}:1:other"), headers=H)
    assert bad.status_code == 400
    forged = await api.post(_url(WS), json=await _body(tid, operands={**OPERANDS, "to": "evil@x.io"}), headers=H)
    assert forged.status_code == 400 and forged.json()["detail"] == "operands_hash_mismatch"  # hash recomputed here
    assert await effects_repo.get_effect(WS, f"{tid}:1:c1") is None
    big = await api.post(_url(WS), json=await _body(tid, operands={"body": "x" * 17_000}), headers=H)
    assert big.status_code == 422


async def test_requires_hook_token_and_switch(api):
    tid = await _started(api)
    assert (await api.post(_url(WS), json=await _body(tid))).status_code in (401, 403)
    api.switch = False
    assert (await api.post(_url(WS), json=await _body(tid), headers=H)).status_code == 404
    assert (
        await api.post(
            f"{_url(WS)}/{tid}:1:c1/done", json={"disposition": "done", "runSecret": "s", "runId": "r"}, headers=H
        )
    ).status_code == 404


async def test_done_route(api):
    tid = await _started(api)
    body = await _body(tid)
    await effects_repo.put_effect(WS, _row(body))
    done = {"disposition": "unknown", "resultRef": None, "runSecret": body["runSecret"], "runId": body["runId"]}
    not_intent = await api.post(f"{_url(WS)}/{tid}:1:c1/done", json=done, headers=H)
    assert not_intent.status_code == 409 and not_intent.json()["detail"]["state"] == "proposed"
    await effects_repo.set_effect_state(WS, f"{tid}:1:c1", to="approved")
    assert await effects_repo.use_approval(WS, f"{tid}:1:c1", HASH)
    wrong = await api.post(f"{_url(WS)}/{tid}:1:c1/done", json={**done, "runSecret": "x"}, headers=H)
    assert wrong.status_code == 403
    ok = await api.post(f"{_url(WS)}/{tid}:1:c1/done", json=done, headers=H)
    assert ok.status_code == 200 and ok.json()["state"] == "unknown"
    again = await api.post(f"{_url(WS)}/{tid}:1:c1/done", json=done, headers=H)
    assert again.status_code == 200  # the bridge's retry
    missing = await api.post(f"{_url(WS)}/{tid}:1:zz/done", json=done, headers=H)
    assert missing.status_code == 404


async def test_run_event_waiting_approval_terminal(api):
    tid = await _started(api)
    body = {
        "runSecret": await _secret(tid),
        "taskId": tid,
        "attempt": 1,
        "kind": "terminal",
        "status": "waiting_approval",
        "output": "",
    }
    run_id = (await tasks_repo.get_attempt(WS, tid, 1))["runId"]
    resp = await api.post(f"/api/v1/internal/runtime/{WS}/runs/{run_id}/events", json=body, headers=H)
    assert resp.status_code == 200, resp.text
    assert ("complete", WorkerTurnOut("waiting_approval", None)) in api.fake.log


def test_run_request_wire_carries_effects_and_faults(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    off = runtime_runs.RunRequest(input="x", instructions="y").to_wire()
    assert off["effects"] == "off" and "faults" not in off
    on = runtime_runs.RunRequest(input="x", instructions="y", effects="required", faults=("kill_after_effect_intent",))
    assert on.to_wire()["effects"] == "required" and on.to_wire()["faults"] == ["kill_after_effect_intent"]
    with pytest.raises(ValueError):
        runtime_runs.RunRequest(input="x", instructions="y", effects="maybe")


def test_faults_are_dev_only(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    runtime_runs.RunRequest(input="x", instructions="y", faults=("kill_after_effect_intent",))
    for env in ("prod", "staging", ""):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
        with pytest.raises(ValueError):
            runtime_runs.RunRequest(input="x", instructions="y", faults=("kill_after_effect_intent",))
        runtime_runs.RunRequest(input="x", instructions="y")  # no faults: fine anywhere


async def test_own_approved_call_is_not_rerun(api):
    """The Worker re-posts the very call that was approved (same toolCallId): Landing owns it; no second run."""
    tid = await _started(api)
    await api.post(_url(WS), json=await _body(tid), headers=H)
    await effects_repo.set_effect_state(WS, f"{tid}:1:c1", to="approved", fields={"questionId": "q-a"})
    resp = await api.post(_url(WS), json=await _body(tid), headers=H)
    assert resp.json()["decision"] == "refuse" and "landed" in resp.json()["message"]
    assert (await effects_repo.get_effect(WS, f"{tid}:1:c1"))["state"] == "approved"  # not consumed, not intent


def test_operands_hash_matches_the_bridge():
    assert (
        ri.operands_hash("mail.send", {"b": 1, "a": "x"})
        == hashlib.sha256(b'{"a":"x","b":1,"kind":"mail.send"}').hexdigest()
    )
