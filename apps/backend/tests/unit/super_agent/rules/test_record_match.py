"""Observe records a rule match and nothing else (wave2-S Task 4, spec 12 §3.4)."""

import pytest
from boto3.dynamodb.conditions import Key

from core import tasks_schema as ts
from core.repositories import automations_repo
from core.temporal import registry, rules_activities

pytestmark = pytest.mark.asyncio
WS = "org_a"


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(rules_activities, "put_metric", lambda name, **kw: seen.append((name, kw["dimensions"])))
    return seen


async def _rule(mode="observe"):
    row, _ = await automations_repo.create_schedule(
        WS,
        {
            "scheduleId": "s1",
            "tz": "UTC",
            "policyText": "When someone reports a bug, reproduce it",
            "name": "reproduce the bug",
            "ownerUserId": "user_1",
            "match": {"sources": ["room"], "roomIds": ["r1"]},
            "mode": mode,
        },
    )
    return row


def _rows(table):
    return table.query(KeyConditionExpression=Key("pk").eq(ts.ws_pk(WS)))["Items"]


async def _record(thread="room:r1:m1"):
    return await rules_activities.record_match(
        WS, "s1", 1, thread, {"roomId": "r1", "messageId": thread[-2:]}, 0.9, "Would reproduce the bug", "chat"
    )


@pytest.mark.parametrize("mode", ["observe", "auto"])
async def test_observe_records_only(table, metrics, mode):
    """`test_auto_row_still_only_records` is the auto case: a hand-set auto row writes exactly what observe does."""
    await _rule(mode)
    assert await _record() == "observed"
    sks = sorted(r["sk"] for r in _rows(table))
    assert sks == ["OBS#room:r1:m1", "SCHED#s1", f"TRIG#{ts.rule_trigger_id('s1', 'room:r1:m1')}"]
    trig = next(r for r in _rows(table) if r["sk"].startswith("TRIG#"))
    assert trig["ruleMatch"]["decision"] == "observed" and trig["admission"] == "candidate"
    row = await automations_repo.get_schedule(WS, "s1")
    assert [m["threadKey"] for m in row["stats"]["matches"]] == ["room:r1:m1"]
    assert metrics == [("rules.match", {"outcome": "observed"})]


async def test_record_match_dedupes_per_thread(table, metrics):
    await _rule()
    assert await _record() == "observed"
    assert await _record() == "duplicate"
    row = await automations_repo.get_schedule(WS, "s1")
    assert len(row["stats"]["matches"]) == 1
    assert len([r for r in _rows(table) if r["sk"].startswith("TRIG#")]) == 1


async def test_matches_trimmed_to_50(table, metrics):
    await _rule()
    for i in range(55):
        await _record(f"room:r1:m{i:02d}")
    matches = (await automations_repo.get_schedule(WS, "s1"))["stats"]["matches"]
    assert len(matches) == 50
    assert matches[0]["threadKey"] == "room:r1:m05" and matches[-1]["threadKey"] == "room:r1:m54"


async def test_list_rules_only_rule_rows(table):
    await _rule()
    await automations_repo.create_schedule(
        WS, {"scheduleId": "plain", "tz": "UTC", "policyText": "x", "name": "x", "ownerUserId": "user_1"}
    )
    assert [r["scheduleId"] for r in await automations_repo.list_rules(WS)] == ["s1"]


def test_registered():
    assert rules_activities.record_match in registry.ALL_ACTIVITIES
