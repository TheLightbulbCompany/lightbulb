"""The rule hook on admission's slow path (wave2-S Task 4b, contracts §18): in-scope rules ride the one Jev call,
a confident pick is recorded, nothing runs and routing is unchanged. Built on O6's admission harness."""

import logging
from unittest.mock import AsyncMock

import pytest
from boto3.dynamodb.conditions import Key

from core import tasks_schema as ts
from core.repositories import automations_repo
from core.repositories import tasks_repo as repo
from core.repositories.tasks_repo import VersionConflict
from core.services import admission
from core.temporal import rules_activities
from tests.unit.super_agent.orchestration.test_admission import CEO, FIN, ROOM, WS, _admit, _msg, world  # noqa: F401
from tests.unit.super_agent.orchestration.test_tasks_repo import table  # noqa: F401

pytestmark = pytest.mark.asyncio
VERDICT = {"to": "none", "specialist": None, "confidence": 0.2}


@pytest.fixture
def hooked(world, monkeypatch):  # noqa: F811
    """`world` plus a route fake that records its kwargs and answers `world["rule_answer"]` when rules ride."""
    world["calls"] = []
    world["rule_answer"] = {"rule": "s1", "ruleP": 0.9}
    world["metrics"] = []

    async def route(meta, msg, members, history, specialists, **kw):
        world["calls"].append(kw)
        if kw.get("rules"):
            return {**world["jev_answer"], **world["rule_answer"]}
        return world["jev_answer"]

    world["jev_answer"] = dict(VERDICT)
    monkeypatch.setattr(admission.jev, "route_for_router", route)
    monkeypatch.setattr(
        admission, "put_metric", lambda name, **kw: world["metrics"].append((name, kw.get("dimensions")))
    )
    monkeypatch.setattr(rules_activities, "put_metric", lambda *a, **k: None)
    return world


async def _rule(sid="s1", *, mode="observe", room=ROOM):
    await automations_repo.create_schedule(
        WS,
        {
            "scheduleId": sid,
            "name": "reproduce the bug",
            "ownerUserId": "user_sam",
            "cron": [],
            "tz": "UTC",
            "policyText": "When someone reports a bug here, reproduce it",
            "match": {"sources": ["room"], "roomIds": [room]},
            "mode": mode,
        },
    )
    await automations_repo.set_status(WS, sid, "active", reason=None, allowed_from=frozenset({"draft"}))


def _sks(table, prefix):  # noqa: F811
    rows = table.query(KeyConditionExpression=Key("pk").eq(ts.ws_pk(WS)))["Items"]
    return sorted(r["sk"] for r in rows if r["sk"].startswith(prefix))


@pytest.mark.parametrize("mode", ["observe", "auto"])
async def test_observe_records_only(hooked, table, mode):  # noqa: F811
    """`test_auto_row_still_only_records`: a row hand-set to auto writes exactly what observe writes."""
    await _rule(mode=mode)
    msg = _msg()
    await _admit(hooked, msg)
    thread_key = f"room:{ROOM}:{msg['messageId']}"
    assert _sks(table, "TRIG#") == [f"TRIG#{ts.rule_trigger_id('s1', thread_key)}"]  # no chat trigger: Jev said none
    assert _sks(table, "OBS#") == [f"OBS#{thread_key}"] and _sks(table, "TASK#") == []
    assert hooked["router"] == [] and hooked["inputs"] == []
    (entry,) = (await automations_repo.get_schedule(WS, "s1"))["stats"]["matches"]
    assert entry["wouldDo"] == "Would reproduce the bug for: “pay acme”" and entry["version"] == 1
    assert [c.get("rules", [None])[0]["scheduleId"] for c in hooked["calls"]] == ["s1"]


async def test_rule_match_never_changes_routing(hooked, table):  # noqa: F811
    await _rule()
    hooked["jev_answer"] = {"to": "router", "specialist": FIN, "confidence": 0.9}
    tid = await _admit(hooked, _msg())
    trig = await repo.get_trigger(WS, tid)
    assert trig["admission"] == "candidate" and trig["routedTo"] == {"specialist": FIN}
    assert len(hooked["router"]) == 1
    assert len(_sks(table, "TRIG#rule:")) == 1


async def test_hook_skips_addressed_and_bots(hooked, table):  # noqa: F811
    await _rule()
    await _admit(hooked, _msg(targets=[{"agentId": CEO, "eventKind": "user_request"}]))
    await _admit(hooked, {**_msg(2), "author": {"kind": "agent", "id": CEO}})
    assert hooked["calls"] == [] and _sks(table, "TRIG#rule:") == []


async def test_hook_no_rules_unchanged(hooked, table):  # noqa: F811
    await _admit(hooked, _msg())
    assert hooked["calls"] == [{}]  # the kwargs O6 sends, nothing added
    await _rule(room="other-room")
    await _admit(hooked, _msg(2))
    assert hooked["calls"] == [{}, {}] and _sks(table, "TRIG#rule:") == []


async def test_jev_unavailable_records_nothing(hooked, table):  # noqa: F811
    await _rule()
    hooked["jev_answer"] = {"fallback": True}
    hooked["rule_answer"] = {}
    await _admit(hooked, _msg())
    assert _sks(table, "TRIG#") == [] and _sks(table, "OBS#") == [] and hooked["router"] == []
    assert ("rules.match", {"outcome": "jev_unavailable"}) in hooked["metrics"]


async def test_bad_who_still_records_the_rule(hooked, table):  # noqa: F811
    await _rule()
    hooked["jev_answer"] = {"fallback": True}
    await _admit(hooked, _msg())
    assert len(_sks(table, "TRIG#rule:")) == 1 and hooked["router"] == []


async def test_no_pick_is_no_record(hooked, table):  # noqa: F811
    await _rule()
    hooked["rule_answer"] = {"rule": None, "ruleP": None}
    await _admit(hooked, _msg())
    assert _sks(table, "TRIG#rule:") == [] and ("rules.match", {"outcome": "no_match"}) in hooked["metrics"]


async def test_thread_reply_keys_on_the_root(hooked, table):  # noqa: F811
    await _rule()
    root = _msg(1)["messageId"]
    await _admit(hooked, _msg(2, root=root))
    await _admit(hooked, _msg(3, root=root))  # a second message in the thread: the same record
    assert _sks(table, "TRIG#rule:") == [f"TRIG#{ts.rule_trigger_id('s1', f'room:{ROOM}:{root}')}"]


async def test_human_stopped_thread_is_skipped(hooked, table):  # noqa: F811
    await _rule()
    root = _msg(1)["messageId"]
    thread_key = f"room:{ROOM}:{root}"
    task, _ = await repo.create_task(
        WS,
        {
            "taskId": ts.new_task_id(),
            "identifier": "LB-1",
            "title": "Fix it",
            "status": "queued",
            "audience": {"kind": "room", "roomId": ROOM},
            "lane": "shared",
            "ownerUserId": "user_sam",
            "triggerId": ts.trigger_id("chat", root),
        },
    )
    await repo.subscribe(WS, task["taskId"], thread_key, created_by="router")
    await repo.transition_task(WS, task["taskId"], to="working")
    await repo.transition_task(WS, task["taskId"], to="resolved", reason="stopped")
    await repo.unsubscribe(WS, task["taskId"], thread_key)
    await _admit(hooked, _msg(2, root=root))
    assert _sks(table, "TRIG#rule:") == [] and ("rules.match", {"outcome": "human_stopped"}) in hooked["metrics"]


async def test_record_conflict_is_logged_not_raised(hooked, table, monkeypatch, caplog):  # noqa: F811
    await _rule()
    monkeypatch.setattr(rules_activities, "record_match", AsyncMock(side_effect=VersionConflict("moved")))
    with caplog.at_level(logging.WARNING):
        await _admit(hooked, _msg())
    assert "record_match conflict" in caplog.text and _sks(table, "TRIG#rule:") == []
