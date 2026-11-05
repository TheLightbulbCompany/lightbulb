"""§18 rule fields on the automation row, rule trigger ids, the OBS# marker (wave2-S Task 1)."""

from core import tasks_schema as ts

NOW = "2026-11-04T12:00:00Z"


def _sched(**extra):
    return ts.Schedule(
        ws="org_a",
        schedule_id="s1",
        tz="UTC",
        policy_text="When someone reports a bug, reproduce it",
        name="Bug repro",
        owner_user_id="user_1",
        created_at=NOW,
        updated_at=NOW,
        **extra,
    )


def test_rule_trigger_id_deterministic():
    tid = ts.rule_trigger_id("s1", "room:r:m")
    assert tid == ts.rule_trigger_id("s1", "room:r:m")
    assert tid.startswith("rule:s1:")
    assert len(tid.removeprefix("rule:s1:")) == 16
    assert tid != ts.rule_trigger_id("s1", "room:r:m2")


def test_schedule_rule_fields_roundtrip():
    row = _sched(match={"sources": ["room"], "roomIds": ["r1"]}, mode="observe")
    item = row.to_item()
    assert item["match"]["roomIds"] == ["r1"]
    assert item["dailyCapUsd"] == 10.0
    assert ts.Schedule.from_item(item) == row


def test_schedule_without_match_unchanged():
    dumped = _sched().model_dump(by_alias=True, exclude_none=True)
    assert "match" not in dumped and "stats" not in dumped


def test_observed_key():
    assert ts.observed_sk("room:r:m") == "OBS#room:r:m"
    row = ts.Observed(
        ws="org_a",
        thread_key="room:r:m",
        sched_id="s1",
        trigger_id=ts.rule_trigger_id("s1", "room:r:m"),
        at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    assert row.to_item()["sk"] == "OBS#room:r:m"


def test_trigger_carries_rule_match():
    trig = ts.Trigger(
        ws="org_a",
        trigger_id=ts.rule_trigger_id("s1", "room:r:m"),
        source="chat",
        admission="candidate",
        lane="shared",
        received_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        rule_match={"schedId": "s1", "version": 1, "p": 0.9, "decision": "observed", "wouldDo": "Would x"},
    )
    assert trig.to_item()["ruleMatch"]["wouldDo"] == "Would x"


def test_rule_version_is_not_the_row_lock():
    item = _sched(match={"sources": ["room"], "roomIds": ["r1"]}).to_item()
    assert item["ruleVersion"] == 1 and item["version"] == 1
