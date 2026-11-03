"""A rules change re-decides every held task under the new document (store first, then re-decide)."""

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from core.repositories import a2a_repo
from core.services import a2a_tasks
from core.services.a2a_rules import Rules
from core.services.a2a_authority import operands_hash
from tests.unit.routers.test_a2a_hold import (  # noqa: F401 (hold_env is a fixture)
    AGENT,
    ORG,
    OWNER,
    REQUESTER,
    RULES,
    URI,
    as_user,
    decide,
    get,
    hold_env,
    records,
    send,
)

SECOND = "user_second"


def when_text(rule_id, text, effect):
    return {
        "id": rule_id,
        "resource": "agent:*",
        "skill": "message",
        "requesters": [f"org:{ORG}"],
        "effect": effect,
        "when": {"operandsEqual": {"text": text}},
    }


@pytest.fixture
def env(hold_env):  # noqa: F811
    def _agent(owner_id, req):
        return SimpleNamespace(message_id=str(uuid.uuid4()))

    hold_env.notify.side_effect = _agent  # a distinct message id per notification
    return hold_env


def put_rules(client, doc):
    r = client.put(f"/a2a/{ORG}/rules", json=doc)
    assert r.status_code == 200
    return r.json()["revision"]


def notified(env, task_id):
    return [c.args[1].event_id for c in env.notify.await_args_list if f":{task_id}:" in c.args[1].event_id]


def test_rules_change_redecides_three_holds_into_allow_deny_and_hold(client, app, env):
    allow_id = send(client, text="allow me")["id"]
    deny_id = send(client, text="deny me")["id"]
    hold_id = send(client, text="hold me")["id"]
    old_note = asyncio.run(a2a_repo.get_task(ORG, hold_id))["notificationMessageIds"][OWNER]
    changed = {
        **RULES,
        "holders": {"agent:*": [OWNER, SECOND]},
        "rules": [when_text("ok", "allow me", "allow"), when_text("no", "deny me", "deny")],
    }
    rev = put_rules(client, changed)
    assert asyncio.run(a2a_repo.get_rules(ORG))["revision"] == rev  # stored

    allowed = get(client, allow_id)
    assert allowed["status"]["state"] == "TASK_STATE_WORKING" and allowed["metadata"][URI]["rule"] == "ok"
    env.send.assert_awaited_once()
    assert env.send.await_args.args[0].user_id == REQUESTER and env.send.await_args.args[2].text == "allow me"
    recs = records(client, allow_id)
    assert [(x["decision"], x["rule"], x["policy_revision"]) for x in recs][1:] == [("allow_by_policy", "ok", rev)]
    exc = asyncio.run(
        a2a_repo.get_exception(
            ORG, f"agent:{AGENT}", "message", REQUESTER, operands_hash({"text": "allow me"}), int(time.time())
        )
    )
    assert exc is None  # the policy allowed it: no holder exception

    denied = get(client, deny_id)
    assert denied["status"]["state"] == "TASK_STATE_REJECTED" and denied["metadata"][URI]["rule"] == "no"
    assert [(x["decision"], x["rule"]) for x in records(client, deny_id)] == [
        ("hold", "default"),
        ("deny_by_policy", "no"),
    ]

    assert get(client, hold_id)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    row = asyncio.run(a2a_repo.get_task(ORG, hold_id))
    assert row["holders"] == [OWNER, SECOND] and row["policyRevision"] == rev
    [_, redecided] = records(client, hold_id)
    assert redecided["decision"] == "redecided" and redecided["holder"] == [OWNER, SECOND]
    assert redecided["policy_revision"] == rev
    assert f"a2a:{hold_id}:hold:{SECOND}" in notified(env, hold_id)
    new_note = row["notificationMessageIds"][SECOND]
    assert asyncio.run(a2a_repo.get_task_by_notification(ORG, new_note))[0] == hold_id
    assert asyncio.run(a2a_repo.get_task_by_notification(ORG, old_note))[0] == hold_id  # old card still answers


def test_hold_whose_holder_set_is_unchanged_is_not_renotified(client, app, env):
    task_id = send(client)["id"]
    before = notified(env, task_id)
    rev = put_rules(client, {**RULES, "rules": [when_text("other", "something else", "allow")]})
    assert notified(env, task_id) == before
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    assert row["state"] == "held" and row["holders"] == [OWNER] and row["policyRevision"] == rev
    assert [x["decision"] for x in records(client, task_id)] == ["hold", "redecided"]


def test_redecide_to_allow_whose_dispatch_fails_is_failed_not_stuck(client, app, env):
    task_id = send(client)["id"]
    env.send.side_effect = RuntimeError("runtime down")
    put_rules(client, {**RULES, "default": "allow"})
    t = get(client, task_id)
    assert t["status"]["state"] == "TASK_STATE_FAILED" and t["metadata"][URI]["reason"] == "dispatch_failed"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("allow_by_policy", "default"),
        ("failed", "dispatch_failed"),
    ]


def test_decide_under_a_newer_revision_that_still_holds_redecides_inline_and_proceeds(client, app, env):
    task_id = send(client)["id"]
    # stored after the hold without a put_rules re-decide (the race decide's revision check catches)
    asyncio.run(a2a_repo.put_rules(ORG, {**RULES, "revision": "r-2", "holders": {"agent:*": [OWNER, SECOND]}}))
    with as_user(app, OWNER):
        r = decide(client, task_id)
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_WORKING"
    env.send.assert_awaited_once()
    assert [(x["decision"], x["policy_revision"]) for x in records(client, task_id)] == [
        ("hold", "r-1"),
        ("redecided", "r-2"),
        ("allow_by_holder", "r-2"),
    ]
    assert f"a2a:{task_id}:hold:{SECOND}" in notified(env, task_id)


def test_decide_under_a_newer_revision_that_denies_rejects_and_is_409_not_held(client, app, env):
    task_id = send(client)["id"]
    deny = {"id": "no", "resource": "agent:*", "skill": "message", "requesters": [f"org:{ORG}"], "effect": "deny"}
    asyncio.run(a2a_repo.put_rules(ORG, {**RULES, "revision": "r-2", "rules": [deny]}))
    with as_user(app, OWNER):
        r = decide(client, task_id)
    assert r.status_code == 409 and r.json()["error"]["message"] == "not_held"
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_REJECTED"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("deny_by_policy", "no"),
    ]
    env.send.assert_not_awaited()


def test_pre_1b_row_without_inputs_is_canceled_policy_changed(client, app, env):
    task_id = send(client)["id"]
    item = {**asyncio.run(a2a_repo.get_task(ORG, task_id)), "pk": f"A2A#{ORG}", "sk": f"TASK#{task_id}"}
    for k in ("requesterOrg", "chainAgents", "sessionPerson", "originIsPerson", "verifiedBy"):
        item.pop(k, None)
    a2a_repo._table().put_item(Item=item)  # a phase-1 row: none of the stored decide inputs
    put_rules(client, {**RULES, "default": "allow"})
    t = get(client, task_id)
    assert t["status"]["state"] == "TASK_STATE_CANCELED" and t["metadata"][URI]["reason"] == "policy_changed"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("canceled", "policy_changed"),
    ]
    env.send.assert_not_awaited()


def test_expired_hold_still_cancels_as_expired(client, app, env, monkeypatch):
    task_id = send(client)["id"]
    later = int(time.time()) + 86_401
    monkeypatch.setattr("core.services.a2a_tasks.time", SimpleNamespace(time=lambda: later))
    put_rules(client, {**RULES, "default": "allow"})
    t = get(client, task_id)
    assert t["status"]["state"] == "TASK_STATE_CANCELED" and t["metadata"][URI]["reason"] == "expired"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("canceled", "expired"),
    ]
    env.send.assert_not_awaited()


def test_hold_with_no_holders_is_rejected_no_holder(client, app, env):
    task_id = send(client)["id"]
    put_rules(client, {**RULES, "holders": {"agent:*": []}})
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_REJECTED"
    assert [(x["decision"], x["rule"]) for x in records(client, task_id)] == [
        ("hold", "default"),
        ("deny_by_policy", "no_holder"),
    ]
    env.send.assert_not_awaited()


def test_a_failed_redecide_does_not_strand_the_others_and_a_retried_put_repairs_it(client, app, env, monkeypatch):
    ids = [send(client, text="one")["id"], send(client, text="two")["id"]]
    real = a2a_repo.get_exception
    calls = []

    async def _flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("table blip")
        return await real(*a, **k)

    monkeypatch.setattr(a2a_repo, "get_exception", _flaky)
    changed = {**RULES, "holders": {"agent:*": [OWNER, SECOND]}}
    r = client.put(f"/a2a/{ORG}/rules", json=changed)
    assert r.status_code == 200
    rev = r.json()["revision"]
    revs = sorted(asyncio.run(a2a_repo.get_task(ORG, i))["policyRevision"] for i in ids)
    assert revs.count(rev) == 1  # one re-decided, one left under the old revision
    assert put_rules(client, changed) == rev  # the same document again
    for i in ids:
        row = asyncio.run(a2a_repo.get_task(ORG, i))
        assert row["state"] == "held" and row["policyRevision"] == rev and row["holders"] == [OWNER, SECOND]
        assert [x["decision"] for x in records(client, i)] == ["hold", "redecided"]


def test_redecide_under_a_superseded_document_leaves_the_row_alone(client, app, env):
    task_id = send(client)["id"]
    row = asyncio.run(a2a_repo.get_task(ORG, task_id))
    allow_all = when_text("ok", "please do the thing", "allow")
    stale = Rules.model_validate({**RULES, "revision": "older", "rules": [allow_all]})  # stored is r-1
    assert asyncio.run(a2a_tasks._redecide(ORG, row, stale, now=int(time.time()))) == row
    assert [x["decision"] for x in records(client, task_id)] == ["hold"]
    assert get(client, task_id)["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    env.send.assert_not_awaited()
