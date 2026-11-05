"""Calendar invites from the watch's sync token, and the hook-fire branch for a calendar rule (wave2-S Task 7b)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from boto3.dynamodb.conditions import Key

from core import tasks_schema as ts
from core.repositories import automations_repo
from core.temporal import registry, rules_activities

pytestmark = pytest.mark.asyncio
WS, PID, ME = "org_1", "pid1", "sam@acme.com"


def _event(eid="e1", **over):
    return {
        "id": eid,
        "status": "confirmed",
        "summary": "Q4 review",
        "organizer": {"email": "boss@acme.com"},
        "attendees": [{"email": ME, "responseStatus": "needsAction"}],
        "start": {"dateTime": "2099-11-06T15:00:00Z"},
    } | over


async def _hook(**over):
    row = {
        "publicId": PID,
        "scheduleId": "s1",
        "provider": "google",
        "events": ["calendar.changed"],
        "signingMode": "bearer",
        "secretRef": f"hook-{PID}",
        "armed": True,
        "account": ME,
    } | over
    await automations_repo.put_hook(WS, row)


async def _rule(sid="s1", *, match=None, mode="observe", status="active"):
    await automations_repo.create_schedule(
        WS,
        {
            "scheduleId": sid,
            "name": "Invite prep",
            "ownerUserId": "user_sam",
            "cron": [],
            "tz": "America/New_York",
            "policyText": "p",
            "mode": mode,
            **({"match": match} if match else {}),
        },
    )
    if status != "draft":
        await automations_repo.set_status(WS, sid, status, reason=None, allowed_from=frozenset({"draft"}))


@pytest.fixture
def gog(table):
    run = MagicMock()
    with (
        patch("core.services.google_watch._gog_env", return_value={"FAKE": "env"}),
        patch("core.services.google_watch._run_gog", run),
    ):
        yield run


def _params(call) -> dict:
    argv = call.args[0]
    assert argv[:6] == ["gog", "api", "call", "calendar", "v3", "events.list"] and argv[-2:] == ["-a", ME]
    return json.loads(argv[7])


async def test_first_sync_stores_the_token_and_matches_nothing(gog):
    await _hook()
    gog.return_value = json.dumps({"items": [_event()], "nextSyncToken": "t1"})
    assert await rules_activities.calendar_invites(WS, PID) == []
    assert (await automations_repo.get_hook(WS, PID))["pageToken"] == "t1"
    params = _params(gog.call_args)
    assert "timeMin" in params and "syncToken" not in params


async def test_calendar_invite_match(gog):
    """Four events since the last sync, over two pages: only the open invite from someone else matches."""
    await _hook(pageToken="t1")
    needs, own, accepted, past = (
        _event("e1"),
        _event("e2", organizer={"email": ME}),
        _event("e3", attendees=[{"email": ME, "responseStatus": "accepted"}]),
        _event("e4", start={"dateTime": "2020-01-01T00:00:00Z"}),
    )
    gog.side_effect = [
        json.dumps({"items": [needs, own], "nextPageToken": "p2"}),
        json.dumps({"items": [accepted, past], "nextSyncToken": "t2"}),
    ]
    assert [e["id"] for e in await rules_activities.calendar_invites(WS, PID)] == ["e1"]
    first, second = gog.call_args_list
    assert _params(first) == {"calendarId": "primary", "syncToken": "t1"}
    assert _params(second) == {"calendarId": "primary", "syncToken": "t1", "pageToken": "p2"}
    assert (await automations_repo.get_hook(WS, PID))["pageToken"] == "t2"


async def test_refused_sync_token_starts_over(gog):
    await _hook(pageToken="stale")
    gog.side_effect = [ValueError("Google refused: 410 Gone"), json.dumps({"items": [_event()], "nextSyncToken": "t3"})]
    assert await rules_activities.calendar_invites(WS, PID) == []
    assert (await automations_repo.get_hook(WS, PID))["pageToken"] == "t3"
    assert "timeMin" in _params(gog.call_args)


async def test_gog_failure_on_first_sync_raises(gog):
    await _hook()
    gog.side_effect = ValueError("Google refused: insufficient scope")
    with pytest.raises(ValueError):
        await rules_activities.calendar_invites(WS, PID)


def _trig_rows(table):
    rows = table.query(KeyConditionExpression=Key("pk").eq(ts.ws_pk(WS)))["Items"]
    return [r for r in rows if r["sk"].startswith("TRIG#")]


async def test_calendar_rule_fire_records_observe(table, monkeypatch):
    await _hook(pageToken="t1")
    await _rule(match={"sources": ["calendar"], "account": ME})
    invites = AsyncMock(return_value=[_event()])
    monkeypatch.setattr(rules_activities, "calendar_invites", invites)
    monkeypatch.setattr(rules_activities, "put_metric", lambda *a, **k: None)
    hook = await automations_repo.get_hook(WS, PID)
    assert await rules_activities.calendar_rule_fire(WS, hook) is True
    (trig,) = _trig_rows(table)
    assert trig["sk"] == f"TRIG#{ts.rule_trigger_id('s1', f'cal:{ME}:e1')}"
    assert (
        trig["source"] == "webhook" and trig["ruleMatch"]["p"] is None and trig["ruleMatch"]["decision"] == "observed"
    )
    (entry,) = (await automations_repo.get_schedule(WS, "s1"))["stats"]["matches"]
    assert entry["wouldDo"] == "Would prep a reply to “Q4 review” (Fri 6 Nov 10:00)"  # 15:00Z in New York
    assert await rules_activities.calendar_rule_fire(WS, hook) is True  # a redelivery records nothing new
    assert len(_trig_rows(table)) == 1


async def test_calendar_rule_fire_off_rule_only_advances_the_sync(table, monkeypatch):
    await _hook(pageToken="t1")
    await _rule(match={"sources": ["calendar"], "account": ME}, mode="off")
    invites = AsyncMock(return_value=[_event()])
    monkeypatch.setattr(rules_activities, "calendar_invites", invites)
    assert await rules_activities.calendar_rule_fire(WS, await automations_repo.get_hook(WS, PID)) is True
    invites.assert_awaited_once()
    assert _trig_rows(table) == []


async def test_calendar_rule_fire_plain_automation_is_not_handled(table, monkeypatch):
    await _hook()
    await _rule()  # a calendar automation someone set up, not a rule
    invites = AsyncMock()
    monkeypatch.setattr(rules_activities, "calendar_invites", invites)
    assert await rules_activities.calendar_rule_fire(WS, await automations_repo.get_hook(WS, PID)) is False
    invites.assert_not_awaited()


def test_registered():
    assert rules_activities.calendar_invites in registry.ALL_ACTIVITIES
