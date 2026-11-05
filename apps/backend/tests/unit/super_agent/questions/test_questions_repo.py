"""questions_repo (wave2-Q Task 2): Question + `NEEDS#` rows. Review focus 1 (two answerers press at once) and
the adversarial cases: a non-answerer, an answer after a cancel, a replayed press."""

import asyncio

import pytest

from core import tasks_schema as ts
from core.repositories import questions_repo as repo

WS = "org_1"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def _q(qid="q-1", **over):
    return {
        "taskId": TASK,
        "qid": qid,
        "kind": "approval",
        "subkind": "purchase",
        "title": "buy a domain for $12",
        "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "askedBy": {"kind": "worker", "attempt": 1},
        "answerers": ["user_sam", "user_ada"],
        "answererRule": "requester+admins",
        "baseAnswerers": ["user_sam"],
    } | over


async def _answer(qid="q-1", by="user_sam", cid="c-1", option="allow_once"):
    return await repo.record_answer(
        WS, TASK, qid, option_id=option, trigger_id=None, by=by, client_answer_id=cid, expected_version=None
    )


async def test_open_question_writes_q_and_needs_atomically(table):
    row, created = await repo.open_question(WS, _q())
    assert created and row["state"] == "open"
    assert [n["qid"] for n in (await repo.needs_for_user(WS, "user_sam"))[0]] == ["q-1"]
    assert [n["qid"] for n in (await repo.needs_for_user(WS, "user_ada"))[0]] == ["q-1"]


async def test_open_question_adds_answerers_to_audience(table):
    from core.repositories import tasks_repo

    await repo.open_question(WS, _q())
    task = await tasks_repo.get_task(WS, TASK)
    assert task["audience"]["userIds"] == ["user_sam", "user_ada"]  # Sam kept, Ada added once
    await repo.open_question(WS, _q())  # a retried open adds nobody twice
    await repo.open_question(WS, _q("q-2", answerers=["user_ada", "user_bo"], baseAnswerers=[]))
    assert (await tasks_repo.get_task(WS, TASK))["audience"]["userIds"] == ["user_sam", "user_ada", "user_bo"]


async def test_open_question_on_a_missing_task_fails(table):
    with pytest.raises(repo.TasksRepoError):
        await repo.open_question(WS, _q(taskId="9f14e45f-ceea-467a-9575-8a1b2c3d4e5f"))
    assert await repo.list_for_task(WS, "9f14e45f-ceea-467a-9575-8a1b2c3d4e5f") == []
    assert (await repo.needs_for_user(WS, "user_sam"))[0] == []


async def test_open_question_idempotent_and_atomic(table):
    await repo.open_question(WS, _q())
    again, created = await repo.open_question(WS, _q(answerers=["user_priya"], baseAnswerers=["user_priya"]))
    assert not created and again["answerers"] == ["user_sam", "user_ada"]
    assert (await repo.needs_for_user(WS, "user_priya"))[0] == []  # the failed transaction wrote no NEEDS#


async def test_concurrent_answers_one_winner(table):
    await repo.open_question(WS, _q())
    results = await asyncio.gather(
        _answer(by="user_sam", cid="c-sam"), _answer(by="user_ada", cid="c-ada"), return_exceptions=True
    )
    rows = [r for r in results if isinstance(r, dict)]
    errors = [r for r in results if isinstance(r, repo.NotOpen)]
    assert len(rows) == 1 and len(errors) == 1
    assert errors[0].state == "answered" and errors[0].by == rows[0]["answeredBy"] and errors[0].at


async def test_answer_retry_same_client_id_returns_row(table):
    await repo.open_question(WS, _q())
    first = await _answer(cid="c-1")
    again = await _answer(cid="c-1")
    assert again["version"] == first["version"] and again["answer"]["optionId"] == "allow_once"
    with pytest.raises(repo.NotOpen):
        await _answer(cid="c-2")


async def test_non_answerer_refused_by_the_row(table):
    await repo.open_question(WS, _q())
    with pytest.raises(repo.NotAnswerer):
        await _answer(by="user_priya", cid="c-p")
    assert (await repo.list_open(WS, TASK))[0]["state"] == "open"


async def test_answer_deletes_needs_rows(table):
    await repo.open_question(WS, _q())
    row = await _answer(option="deny")
    assert row["state"] == "answered" and row["answer"]["optionId"] == "deny"  # denied is an answer
    assert (await repo.needs_for_user(WS, "user_sam"))[0] == []
    assert (await repo.needs_for_user(WS, "user_ada"))[0] == []


async def test_answer_stale_version(table):
    row, _ = await repo.open_question(WS, _q())
    with pytest.raises(repo.VersionConflict):
        await repo.record_answer(
            WS, TASK, "q-1", option_id="deny", trigger_id=None, by="user_sam", client_answer_id="c", expected_version=9
        )
    assert (
        await repo.record_answer(
            WS,
            TASK,
            "q-1",
            option_id="deny",
            trigger_id=None,
            by="user_sam",
            client_answer_id="c",
            expected_version=row["version"],
        )
    )["state"] == "answered"


async def test_answer_must_name_an_offered_option(table):
    await repo.open_question(WS, _q())
    with pytest.raises(repo.InvalidOption):
        await _answer(option="publish")
    assert (await repo.list_open(WS, TASK))[0]["state"] == "open"


async def test_free_text_question_takes_answer_only(table):
    await repo.open_question(WS, _q("q-t", kind="question", options=[]))
    with pytest.raises(repo.InvalidOption):
        await _answer("q-t", option="allow_once")
    assert (await _answer("q-t", option="answer"))["state"] == "answered"


async def test_answer_unknown_question(table):
    with pytest.raises(repo.NotFound):
        await _answer(qid="q-nope")


async def test_cancel_open_marks_cancelled_keeps_row(table):
    await repo.open_question(WS, _q("q-1"))
    await repo.open_question(WS, _q("q-2"))
    await _answer("q-2")
    assert await repo.cancel_open(WS, TASK, by="user_priya", reason="stopped") == ["q-1"]
    rows = {r["qid"]: r for r in await repo.list_for_task(WS, TASK)}
    assert rows["q-1"]["state"] == "cancelled"
    assert (rows["q-1"]["reason"], rows["q-1"]["cancelledBy"]) == ("stopped", "user_priya")
    assert rows["q-2"]["state"] == "answered"
    assert await repo.list_open(WS, TASK) == []
    assert (await repo.needs_for_user(WS, "user_sam"))[0] == []


async def test_answer_after_cancel_not_open(table):
    await repo.open_question(WS, _q())
    await repo.cancel_open(WS, TASK, by="user_priya", reason="stopped")
    with pytest.raises(repo.NotOpen) as err:
        await _answer()
    assert (err.value.state, err.value.by) == ("cancelled", "user_priya")


async def test_needs_for_user_pages(table):
    for i in range(3):
        await repo.open_question(WS, _q(f"q-{i}"))
    page, cursor = await repo.needs_for_user(WS, "user_sam", limit=2)
    rest, end = await repo.needs_for_user(WS, "user_sam", cursor=cursor, limit=2)
    assert len(page) == 2 and len(rest) == 1 and end is None
    assert all(r["sk"].startswith(ts.needs_sk("user_sam", "", "")[:-1]) for r in page + rest)
