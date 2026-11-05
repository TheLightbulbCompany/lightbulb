"""A Google Calendar push for a rule's watch records invites and never starts a workflow (wave2-S Task 7b);
a push for an ordinary calendar automation fires as A7 built it."""

from unittest.mock import AsyncMock, patch

from boto3.dynamodb.conditions import Key

from core import tasks_schema as ts
from core.repositories import automations_repo
from core.temporal import rules_activities
from tests.unit.super_agent.automations.test_hooks import PID, WS, _hook, _post, _s3_keys, client, env  # noqa: F401

ME = "sam@acme.com"
HEADERS = {
    "X-Goog-Channel-Token": "chan-tok",
    "X-Goog-Resource-State": "exists",
    "X-Goog-Channel-ID": "ch1",
    "X-Goog-Message-Number": "7",
}
EVENT = {
    "id": "e1",
    "status": "confirmed",
    "summary": "Q4 review",
    "organizer": {"email": "boss@acme.com"},
    "attendees": [{"email": ME, "responseStatus": "needsAction"}],
    "start": {"dateTime": "2099-11-06T15:00:00Z"},
}


async def _calendar(env, *, rule: bool):  # noqa: F811
    env.secrets[f"hook-{PID}"] = {"secret": "chan-tok", "mode": "bearer"}
    await _hook(provider="google", signingMode="bearer", events=["calendar.changed"], account=ME)
    row = {"scheduleId": "s1", "name": "n", "ownerUserId": "u", "cron": [], "tz": "UTC", "policyText": "p"}
    if rule:
        row |= {"match": {"sources": ["calendar"], "account": ME}, "mode": "observe"}
    await automations_repo.create_schedule(WS, row)
    await automations_repo.set_status(WS, "s1", "active", reason=None, allowed_from=frozenset({"draft"}))


def _trig_rows(table):
    rows = table.query(KeyConditionExpression=Key("pk").eq(ts.ws_pk(WS)))["Items"]
    return [r["sk"] for r in rows if r["sk"].startswith("TRIG#")]


async def test_calendar_rule_push_records_and_never_fires(env, client, table):  # noqa: F811
    await _calendar(env, rule=True)
    with (
        patch.object(rules_activities, "calendar_invites", AsyncMock(return_value=[EVENT])),
        patch.object(rules_activities, "put_metric", lambda *a, **k: None),
    ):
        r = _post(client, b"", HEADERS)
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    assert env.temporal.started == [] and _s3_keys(env) == []
    assert _trig_rows(table) == [f"TRIG#{ts.rule_trigger_id('s1', f'cal:{ME}:e1')}"]


async def test_calendar_rule_push_gog_down_503(env, client, table):  # noqa: F811
    await _calendar(env, rule=True)
    with patch.object(rules_activities, "calendar_invites", AsyncMock(side_effect=ValueError("gog down"))):
        r = _post(client, b"", HEADERS)
    assert r.status_code == 503 and r.headers["Retry-After"] == "30"
    assert env.temporal.started == [] and _trig_rows(table) == []


async def test_calendar_push_for_a_plain_automation_fires_as_before(env, client, table):  # noqa: F811
    await _calendar(env, rule=False)
    with patch.object(rules_activities, "calendar_invites", AsyncMock()) as invites:
        r = _post(client, b"", HEADERS)
    assert r.status_code == 202
    invites.assert_not_awaited()
    assert [s[0] for s in env.temporal.started] == ["TriggerFireWorkflow"] and _trig_rows(table) == []
