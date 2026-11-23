"""lane Q Activities over the moto tasks table (wave2-Q Task 3): open from proposed Effects, relay to answerers
only and once, record an answer with its Effect decision and Trigger, cancel on Stop."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from core.repositories import effects_repo, questions_repo, tasks_repo
from core.services import questions
from core.temporal import task_questions as tq

from .conftest import TASK, WS

SAM, ADA, PRIYA = "org_1", "org_1", "user_priya"
POLICY = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", AsyncMock(return_value=dict(POLICY)))
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


async def test_a_purchase_preview_carries_its_currency(world):
    """Dev walk 7 (W7-9): "11.81" with no currency. The amount is US dollars (request_purchase's contract)."""
    await effects_repo.put_effect(
        WS, _effect(kind="purchase", operands={"merchant": "Porkbun", "amount": "11.81", "url": "u", "why": "w"})
    )
    await _run(tq.open_questions, WS, TASK)
    (q,) = await questions_repo.list_for_task(WS, TASK)
    assert q["title"] == "buy from Porkbun for $11.81"
    assert (q["preview"]["amount"], q["preview"]["currency"]) == ("11.81", "USD")


async def test_open_questions_never_reopens_a_closed_one(world):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    assert await _run(tq.open_questions, WS, TASK) == []


async def test_publish_without_an_app_id_asks_the_admins(world):
    """Gate run 3 (task d3e55e37): an app.publish the classifier could not name an app for opened no Question."""
    await effects_repo.put_effect(WS, _effect(kind="app.publish", operands={}))
    [ref] = await _run(tq.open_questions, WS, TASK)
    assert ref.answerers == [ADA]


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


async def test_relay_survives_an_origin_thread_that_is_not_in_the_origin_room(world, relay):
    """8 Nov prod (task 7f2c3575, a Meet's): the Router's ack went to the meeting's output room, so the origin thread
    is in another room; relay_question failed 5/5 with MESSAGE_NOT_FOUND and no answerer was pushed or emailed."""
    from core.services import messaging

    await tasks_repo.subscribe(WS, TASK, "room:00000000-0000-0000-0000-0000000000aa:ack-elsewhere", created_by="router")
    gone = messaging.ApiProblem("MESSAGE_NOT_FOUND", "Referenced message not found in this room.")

    async def post(ws, room, **k):
        if k.get("thread_root"):
            raise gone
        return "m1"

    relay["post"].side_effect = post
    await effects_repo.put_effect(WS, _effect(kind="purchase", operands={"item": "acme.dev"}))
    [ref] = await _run(tq.open_questions, WS, TASK)
    await _run(tq.relay_question, WS, TASK, ref.qid)
    origin = [c for c in relay["post"].await_args_list if c.kwargs["event_id"].endswith(":origin")]
    assert [c.kwargs["thread_root"] for c in origin] == ["ack-elsewhere", None]  # then the origin room itself
    assert [c.args[0] for c in relay["push"].await_args_list] == [ADA] and relay["away"].await_count == 1


async def test_relay_names_the_specialist_not_its_id(world, relay, monkeypatch):
    """Dev walk 7: the card read "**ee033f05-…** needs your OK to …": the specialist's id, not its name."""
    from core.services import messaging

    spec = "ee033f05-875e-4049-b2d6-2e74309bd5db"
    task = await tasks_repo.get_task(WS, TASK)
    await tasks_repo.update_task_fields(WS, TASK, {"specialist": spec}, expected_version=task["version"])
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": spec, "name": "Engineering"}]))
    await effects_repo.put_effect(WS, _effect(kind="purchase", operands={"item": "acme.dev"}))
    [ref] = await _run(tq.open_questions, WS, TASK)
    await _run(tq.relay_question, WS, TASK, ref.qid)
    assert relay["post"].await_args.kwargs["text"] == "**Engineering** needs your OK to buy acme.dev"


async def test_relay_skips_a_closed_question(world, relay):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    await questions_repo.cancel_open(WS, TASK, by=PRIYA, reason="stopped")
    await _run(tq.relay_question, WS, TASK, ref.qid)
    relay["post"].assert_not_awaited()
    relay["push"].assert_not_awaited()


async def test_cancel_open_questions(world):
    await effects_repo.put_effect(WS, _effect())
    [ref] = await _run(tq.open_questions, WS, TASK)
    assert await _run(tq.cancel_open_questions, WS, TASK, PRIYA, "stopped") == [ref.qid]
    assert (await questions_repo.get_question(WS, TASK, ref.qid))["state"] == "cancelled"


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


async def _ask_trigger(qid, kind="question", subkind="question", title="Which one?"):
    trigger_id = f"ask:{qid}"
    await tasks_repo.put_trigger(
        WS,
        {
            "triggerId": trigger_id,
            "source": "ask",
            "ref": {
                "taskId": TASK,
                "qid": qid,
                "kind": kind,
                "subkind": subkind,
                "title": title,
                "text": "",
                "options": [],
                "blocking": True,
                "attempt": 1,
            },
            "admission": "respond",
            "lane": "shared",
            "receivedAt": "2026-11-05T00:00:00+00:00",
        },
    )
    return trigger_id


async def test_open_asked_question_missing_trigger_fails(world):
    with pytest.raises(ApplicationError):
        await _run(tq.open_asked_question, WS, TASK, tq.AskRef("ask:nope", "question", "question", True))


@pytest.mark.parametrize("retire", ["cancel", "answer", "resolve"])
async def test_relay_rechecks_retirement_after_room_creation(world, relay, monkeypatch, retire):
    from core.services import task_conversation

    await effects_repo.put_effect(WS, _effect(kind="purchase"))
    [ref] = await _run(tq.open_questions, WS, TASK)
    entered, release = asyncio.Event(), asyncio.Event()

    async def room(*_, **__):
        entered.set()
        await release.wait()
        return "room-task"

    monkeypatch.setattr(task_conversation, "ensure_task_room", room)
    running = asyncio.create_task(_run(tq.relay_question, WS, TASK, ref.qid))
    await asyncio.wait_for(entered.wait(), 5)
    if retire == "cancel":
        await questions_repo.cancel_open(WS, TASK, by=SAM, reason="stopped")
    elif retire == "answer":
        await questions_repo.record_answer(
            WS,
            TASK,
            ref.qid,
            option_id="allow_once",
            trigger_id=None,
            by=ADA,
            client_answer_id="during-relay",
            expected_version=None,
        )
    else:
        task = await tasks_repo.get_task(WS, TASK)
        await tasks_repo.transition_task(WS, TASK, to="resolved", expected_version=task["version"])
    release.set()
    await running
    relay["post"].assert_not_awaited()
    relay["push"].assert_not_awaited()
    relay["away"].assert_not_awaited()
