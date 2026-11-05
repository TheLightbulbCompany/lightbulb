"""Did-it-yourself proxy (wave2-S Task 6, spec 12 §3.4)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import tasks_schema as ts
from core.repositories import automations_repo
from core.temporal import rules_activities

from .test_record_match import WS, _record, _rule, metrics  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("metrics")]


def _age(table, thread, hours):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    table.update_item(
        Key={"pk": ts.ws_pk(WS), "sk": ts.observed_sk(thread)},
        UpdateExpression="SET #a = :a",
        ExpressionAttributeNames={"#a": "at"},
        ExpressionAttributeValues={":a": at},
    )


async def _done(thread="room:r1:m1"):
    row = await automations_repo.get_schedule(WS, "s1")
    return {m["threadKey"]: m["doneByHuman"] for m in row["stats"]["matches"]}[thread]


async def test_task_on_observed_thread_marks_done(table):
    await _rule()
    await _record()
    await _record("room:r1:m2")
    _age(table, "room:r1:m1", 1)
    await rules_activities.note_task_started(WS, "room:r1:m1")
    assert await _done() is True
    assert await _done("room:r1:m2") is False


async def test_old_observation_ignored(table):
    await _rule()
    await _record()
    _age(table, "room:r1:m1", 25)
    await rules_activities.note_task_started(WS, "room:r1:m1")
    assert await _done() is False


async def test_unobserved_thread_is_a_no_op(table):
    await _rule()
    await rules_activities.note_task_started(WS, "room:r1:never")


async def test_mark_failure_does_not_fail_apply(table, monkeypatch):
    await _rule()
    await _record()

    async def boom(*a, **k):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(automations_repo, "mark_done_by_human", boom)
    await rules_activities.note_task_started(WS, "room:r1:m1")  # logs, never raises
    assert await _done() is False


async def test_mark_done_by_human_is_idempotent(table):
    await _rule()
    await _record()
    assert await automations_repo.mark_done_by_human(WS, "s1", "room:r1:m1") is True
    assert await automations_repo.mark_done_by_human(WS, "s1", "room:r1:m1") is False
