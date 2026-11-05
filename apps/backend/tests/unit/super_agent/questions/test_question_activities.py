"""lane Q Activities over the moto tasks table (wave2-Q Task 3): open from proposed Effects, relay to answerers
only and once, record an answer with its Effect decision and Trigger, cancel on Stop."""

from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from core.repositories import effects_repo, questions_repo, tasks_repo
from core.services import questions
from core.temporal import task_questions as tq

from .conftest import TASK, WS

SAM, ADA, PRIYA = "user_sam", "user_ada", "user_priya"
POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
    members = [{"user_id": SAM, "role": "org:member"}, {"user_id": ADA, "role": "org:admin"}]
    monkeypatch.setattr(questions.clerk_admin, "list_organization_members", AsyncMock(return_value=members))
    return table


def _effect(n=1, call="c1", kind="mail.send", **over):
    return {
        "idemKey": f"{TASK}:{n}:{call}",
        "taskId": TASK,
        "attempt": n,
        "kind": kind,
        "tool": "terminal",
        "operands": {"to": "acme@example.com", "subject": "Q3 invoice", "body": "hi"},
        "operandsHash": f"h-{call}",
        "state": "proposed",
        "executorOk": True,
    } | over


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


async def test_open_questions_one_per_proposed_effect(world):
    await effects_repo.put_effect(WS, _effect())
    await effects_repo.put_effect(
        WS, _effect(call="c2", kind="purchase", operands={"item": "acme.dev", "amount": "$12"})
    )
    await effects_repo.put_effect(WS, _effect(call="c3", state="intent"))  # not waiting on anyone
    refs = await _run(tq.open_questions, WS, TASK)
    assert [r.answerers for r in refs] == [[SAM], [ADA]]
    rows = {q["qid"]: q for q in await questions_repo.list_for_task(WS, TASK)}
    mail = rows[questions.qid_for(f"{TASK}:1:c1")]
    assert (mail["kind"], mail["subkind"], mail["title"]) == ("review", "send", "send 'Q3 invoice' to acme@example.com")
    assert [o["id"] for o in mail["options"]] == ["send", "request_changes"]
    assert mail["effectIdemKey"] == f"{TASK}:1:c1" and mail["preview"]["subject"] == "Q3 invoice"
    assert (await tasks_repo.get_task(WS, TASK))["audience"]["userIds"] == [SAM, ADA]
    assert len(await _run(tq.open_questions, WS, TASK)) == 2  # idempotent
    assert len(await questions_repo.list_for_task(WS, TASK)) == 2


async def test_open_questions_never_reopens_a_closed_one(world):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    assert await _run(tq.open_questions, WS, TASK) == []


async def test_open_questions_no_answerer(world, monkeypatch):
    monkeypatch.setattr(questions.clerk_admin, "list_organization_members", AsyncMock(return_value=[]))
    await effects_repo.put_effect(WS, _effect(kind="purchase"))
    with pytest.raises(ApplicationError) as err:
        await _run(tq.open_questions, WS, TASK)
    assert err.value.type == "NoAnswerer" and err.value.non_retryable


@pytest.fixture
def relay(monkeypatch):
    from core.services import needs_you_email, push_service, task_conversation, webhook_dedup

    seen: set[str] = set()

    async def dedupe(key, *, source, ttl_seconds=None):
        if key in seen:
            return webhook_dedup.WebhookDedupResult.ALREADY_SEEN
        seen.add(key)
        return webhook_dedup.WebhookDedupResult.RECORDED

    spies = {
        "room": AsyncMock(return_value="room-task"),
        "post": AsyncMock(return_value="m1"),
        "push": AsyncMock(),
        "away": AsyncMock(),
    }
    monkeypatch.setattr(webhook_dedup, "record_event_or_skip", dedupe)
    monkeypatch.setattr(task_conversation, "ensure_task_room", spies["room"])
    monkeypatch.setattr(task_conversation, "post_as_lightbulb", spies["post"])
    monkeypatch.setattr(push_service, "send_push_to_user", spies["push"])
    monkeypatch.setattr(needs_you_email, "queue_question", spies["away"])
    return spies


async def test_relay_question_idempotent_and_only_to_answerers(world, relay):
    await effects_repo.put_effect(WS, _effect(kind="purchase", operands={"item": "acme.dev"}))
    [ref] = await _run(tq.open_questions, WS, TASK)
    await _run(tq.relay_question, WS, TASK, ref.qid)
    await _run(tq.relay_question, WS, TASK, ref.qid)  # an Activity retry
    events = {c.kwargs["event_id"] for c in relay["post"].await_args_list}
    assert events == {f"q:{ref.qid}"}  # same event id: the message is posted once
    assert relay["post"].await_args.kwargs["task_refs"] == [{"taskId": TASK, "role": "started", "qid": ref.qid}]
    assert [c.args[0] for c in relay["push"].await_args_list] == [ADA]  # one push, the answerer only
    title, body, data = relay["push"].await_args.args[1:]
    assert data == {"source": "task_question", "taskId": TASK, "qid": ref.qid} and len(body) <= 180
    assert relay["away"].await_args.args[3] == [ADA]


async def test_relay_skips_a_closed_question(world, relay):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    await _run(tq.relay_question, WS, TASK, ref.qid)
    relay["post"].assert_not_awaited()
    relay["push"].assert_not_awaited()


@pytest.mark.parametrize(("option", "state"), [("send", "approved"), ("request_changes", "denied")])
async def test_record_answer_decides_the_effect_and_writes_the_trigger(world, option, state):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    out = await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, option, None, SAM, "c-1"))
    land = f"{TASK}:1:c1" if option == "send" else None  # an approved, parsed draft lands; changes go back
    assert out == tq.AnswerOut(f"answer:{ref.qid}:c-1", option, land)
    effect = await effects_repo.get_effect(WS, f"{TASK}:1:c1")
    assert (effect["state"], effect["questionId"]) == (state, ref.qid)
    trigger = await tasks_repo.get_trigger(WS, out.trigger_id)
    assert trigger["ref"] == {"taskId": TASK, "qid": ref.qid, "optionId": option} and trigger["actorUserId"] == SAM
    # The Activity retried after it already ran: same answer back, nothing moves twice.
    assert await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, option, None, SAM, "c-1")) == out


async def test_record_answer_refusals_are_typed(world):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    with pytest.raises(ApplicationError) as err:
        await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, "send", None, PRIYA, "c-p"))
    assert err.value.type == "NotAnswerer"
    with pytest.raises(ApplicationError) as err:
        await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, "allow_once", None, SAM, "c-x"))
    assert err.value.type == "InvalidOption"
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    with pytest.raises(ApplicationError) as err:
        await _run(tq.record_answer, WS, TASK, tq.AnswerRef(ref.qid, "send", None, SAM, "c-1"))
    assert err.value.type == "NotOpen" and err.value.details[:2] == ("cancelled", PRIYA)
    assert (await effects_repo.get_effect(WS, f"{TASK}:1:c1"))["state"] == "proposed"


async def test_cancel_open_questions(world):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    assert await _run(tq.cancel_open_questions, WS, TASK, PRIYA, "stopped") == [ref.qid]
    assert (await questions_repo.get_question(WS, TASK, ref.qid))["state"] == "cancelled"


async def test_no_answerer_on_a_later_effect_writes_nothing(world, monkeypatch):
    """Answerer lists are all computed before any row: a refusal on the 2nd effect leaves no 1st question open."""
    await effects_repo.put_effect(WS, _effect())  # send → Sam
    await effects_repo.put_effect(WS, _effect(call="c2", kind="purchase", operands={"item": "x"}))  # admins
    monkeypatch.setattr(questions.clerk_admin, "list_organization_members", AsyncMock(return_value=[]))
    with pytest.raises(ApplicationError):
        await _run(tq.open_questions, WS, TASK)
    assert await questions_repo.list_for_task(WS, TASK) == []
    assert (await tasks_repo.get_task(WS, TASK))["audience"]["userIds"] == [SAM]  # nobody added


async def test_reminder_says_answer_for_a_plain_question(world, relay):
    await questions_repo.open_question(
        WS,
        {
            "taskId": TASK,
            "qid": "q-ask",
            "kind": "question",
            "title": "Which client?",
            "askedBy": {"kind": "worker"},
            "answerers": [SAM],
            "answererRule": "requester",
            "baseAnswerers": [SAM],
        },
    )
    await _run(tq.remind_question, WS, TASK, "q-ask")
    assert relay["push"].await_args.args[1] == "Still waiting: Lightbulb needs your answer"


async def test_note_answerers_unavailable_posts_once(world, relay):
    await _run(tq.note_answerers_unavailable, WS, TASK)
    kw = relay["post"].await_args.kwargs
    assert kw["text"].startswith("Couldn't find who can approve") and kw["event_id"].startswith("q-lookup-failed:")


async def test_note_answerers_unavailable_missing_task_fails(world, relay):
    with pytest.raises(LookupError):
        await _run(tq.note_answerers_unavailable, WS, "9f14e45f-ceea-467a-9575-8a1b2c3d4e5f")
    relay["post"].assert_not_awaited()
