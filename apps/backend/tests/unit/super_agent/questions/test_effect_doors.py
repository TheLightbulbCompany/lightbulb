"""Backend door gating (wave2-Q Task 12): a task run's door call passes only against an `intent` row of its own
attempt; legacy agents and switched-off workspaces are untouched."""

from unittest.mock import AsyncMock

import pytest

from core import tasks_schema as ts
from core.repositories import effects_repo, tasks_repo
from core.services import effect_doors, questions, super_agent_flags

from .conftest import TASK, WS

AGENT = "agent-finance"
OPS = {"agentId": AGENT, "to": "acme@example.com", "subject": "Q3", "body": "hi"}


@pytest.fixture
def switch(monkeypatch):
    flag = AsyncMock(return_value=True)
    monkeypatch.setattr(super_agent_flags, "flags_for", flag)
    monkeypatch.setattr(effect_doors, "put_metric", lambda *a, **k: None)
    return flag


async def _working(n=1, agent=AGENT, status="running"):
    """The seeded task in `working` with attempt n live for `agent`."""
    from core.dynamodb import get_table

    get_table("tasks").update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.task_sk(TASK)},
        UpdateExpression="SET #s = :s, statusKey = :k, attemptCount = :n",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "working", ":k": ts.status_key(WS, "working"), ":n": n},
    )
    await tasks_repo.put_attempt(
        WS, TASK, n, {"agentId": agent, "sessionId": "s", "status": status, "startedAt": "2026-11-05T00:00:00+00:00"}
    )


async def _effect(n=1, state="intent", kind="mail.send", operands=OPS, call="c1"):
    await effects_repo.put_effect(
        WS,
        {
            "idemKey": f"{TASK}:{n}:{call}",
            "taskId": TASK,
            "attempt": n,
            "kind": kind,
            "tool": "terminal",
            "operands": operands,
            "operandsHash": questions.operands_hash(kind, operands),
            "state": state,
        },
    )


async def test_switch_off_passes_without_reading(table, switch, monkeypatch):
    switch.return_value = False
    spy = AsyncMock(side_effect=AssertionError("tasks read with the switch off"))
    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", spy)
    await effect_doors.require_intent(WS, "mail.send", OPS)


async def test_switch_on_without_any_live_attempt_is_refused(table, switch):
    """No exemption for "not a task run": the hook token is owner-scoped and the body names the agent, so
    `--as <idle specialist>` would otherwise slip through."""
    with pytest.raises(effect_doors.DoorRefused):
        await effect_doors.require_intent(WS, "mail.send", OPS)
    await _working(status="ended")
    with pytest.raises(effect_doors.DoorRefused):
        await effect_doors.require_intent(WS, "mail.send", OPS)


async def test_live_attempt_without_intent_is_refused(table, switch):
    await _working()
    with pytest.raises(effect_doors.DoorRefused) as err:
        await effect_doors.require_intent(WS, "mail.send", OPS)
    assert err.value.code == "effect_not_approved" and err.value.kind == "mail.send"


async def test_matching_intent_of_any_live_attempt_passes_once(table, switch):
    await _working(agent="agent-other")  # the approval is for the operands, whoever's name is on the call
    await _effect()
    await effect_doors.require_intent(WS, "mail.send", OPS)
    row = await effects_repo.get_effect(WS, f"{TASK}:1:c1")
    assert row["doorUsedAt"] and row["state"] == "intent"
    with pytest.raises(effect_doors.DoorRefused):  # the same intent never lets a second call through
        await effect_doors.require_intent(WS, "mail.send", OPS)


async def test_door_use_is_one_conditional_update(table, switch):
    await _working()
    await _effect()
    assert await effects_repo.mark_door_used(WS, f"{TASK}:1:c1") is True
    assert await effects_repo.mark_door_used(WS, f"{TASK}:1:c1") is False
    await _effect(call="c2", state="approved")
    assert await effects_repo.mark_door_used(WS, f"{TASK}:1:c2") is False  # not intent


@pytest.mark.parametrize("state", ["proposed", "approved", "denied", "done", "unknown"])
async def test_only_intent_counts(table, switch, state):
    await _working()
    await _effect(state=state)
    with pytest.raises(effect_doors.DoorRefused):
        await effect_doors.require_intent(WS, "mail.send", OPS)


async def test_other_operands_kind_or_attempt_are_refused(table, switch):
    await _working(n=2)
    await _effect(n=1)  # the earlier attempt's intent
    await _effect(n=2, operands={**OPS, "to": "other@example.com"}, call="c2")  # another recipient
    await _effect(n=2, kind="text.send", operands={"agentId": AGENT, "replyKey": "k", "body": "hi"}, call="c3")
    with pytest.raises(effect_doors.DoorRefused):
        await effect_doors.require_intent(WS, "mail.send", OPS)


async def test_match_predicate_covers_what_the_classifier_cannot_know(table, switch):
    await _working()
    await _effect(kind="app.publish", operands={})  # `lightbulb app publish` with no --app
    await effect_doors.require_intent(
        WS, "app.publish", {"appId": "app_1"}, match=lambda o: o.get("appId") in (None, "app_1")
    )
    await _effect(kind="app.publish", operands={}, call="c2")
    with pytest.raises(effect_doors.DoorRefused):
        await effect_doors.require_intent(
            WS, "app.publish", {"appId": "app_2"}, match=lambda o: o.get("appId") == "app_2"
        )


async def test_live_attempts_walks_every_page(table, switch, monkeypatch):
    pages = {None: ([{"taskId": "t1", "attemptCount": 1}], "c1"), "c1": ([{"taskId": "t2", "attemptCount": 1}], None)}

    async def by_status(ws, status, *, limit, cursor=None):
        return pages[cursor]

    async def attempt(ws, task_id, n):
        return {"taskId": task_id, "n": n, "status": "running", "agentId": AGENT}

    monkeypatch.setattr(tasks_repo, "list_tasks_by_status", by_status)
    monkeypatch.setattr(tasks_repo, "get_attempt", attempt)
    assert [a["taskId"] for a in await effect_doors.live_attempts(WS)] == ["t1", "t2"]


def test_mail_operands_follow_the_classifier():
    assert effect_doors.mail_operands(agent_id="a", subject="S", body_text="hi\n\n", to="x@y.z", reply_key=None) == {
        "agentId": "a",
        "to": "x@y.z",
        "subject": "S",
        "body": "hi",
    }
    assert effect_doors.mail_operands(agent_id="a", subject="", body_text="ok", to=None, reply_key="K1") == {
        "agentId": "a",
        "replyKey": "K1",
        "body": "ok",
    }


def test_operands_hash_matches_the_bridge():
    import hashlib

    assert (
        questions.operands_hash("mail.send", {"b": 1, "a": "x"})
        == hashlib.sha256(b'{"a":"x","b":1,"kind":"mail.send"}').hexdigest()
    )
