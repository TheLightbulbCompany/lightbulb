# ruff: noqa: F811 -- the imported fixtures are the parameters
"""Rule fields on lane A's automation routes (wave2-S Task 5b, contracts §18, spec 12 §3.1-3.2): create / propose
validate the scope, a rule starts in observe, turning on follows the founder's rule, an edit is a new rule."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import automations_repo as repo
from tests.unit.super_agent.automations.test_automations_api import (  # noqa: F401
    BASE,
    PRIYA,
    ROOM,
    SAM,
    WS,
    _as,
    _chat_trigger,
    api,
    internal,
)

pytestmark = pytest.mark.asyncio
RULE = {
    "name": "reproduce the bug",
    "instructions": "When someone reports a bug here, reproduce and fix it quietly",
    "match": {"sources": ["room"], "roomIds": [ROOM]},
}
CAL = {
    "name": "Invite prep",
    "instructions": "Prep replies to invites",
    "match": {"sources": ["calendar"], "account": "priya@x.com"},
}
PROPOSE = f"/api/v1/internal/automations/{WS}/propose"
AUTH = {"Authorization": "Bearer t"}


def _not_member(user):
    async def member(room, kind, uid):
        return None if uid == user else {"id": uid}

    return patch("core.repositories.messaging_repo.get_member", side_effect=member)


async def test_create_rule_is_draft_observe(api):
    r = _as(api, PRIYA, admin=False).post(BASE, json=RULE)  # any member, for a room they are in
    assert r.status_code == 200, r.text
    view = r.json()
    assert (view["status"], view["mode"], view["ruleVersion"]) == ("draft", "observe", 1)
    assert view["match"] == {"sources": ["room"], "roomIds": [ROOM]} and view["schedule"] is None
    assert (view["dailyCapUsd"], view["speculative"], view["builtin"]) == (10.0, False, None)
    assert view["stats"] == {"matches": 0, "matchesThisVersion": 0, "doneByHuman": 0}
    plain = _as(api).post(BASE, json={"name": "n", "instructions": "i"}).json()
    assert plain["match"] is None and plain["stats"] is None and plain["mode"] is None


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({**RULE, "match": {"sources": ["room", "calendar"], "roomIds": [ROOM]}}, "one_source_only"),
        ({**RULE, "match": {"sources": ["room"], "roomIds": []}}, "scope_missing"),
        ({**CAL, "match": {"sources": ["calendar"]}}, "scope_missing"),
        ({**RULE, "builtin": "repro_fix"}, "repo_required"),
    ],
)
async def test_create_rule_scope_errors(api, body, code):
    r = _as(api, PRIYA, admin=False).post(BASE, json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code
    assert await repo.list_schedules(WS) == []


async def test_create_rule_needs_room_membership(api):
    with _not_member(PRIYA):
        r = _as(api, PRIYA, admin=False).post(BASE, json=RULE)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "room_not_member"


async def test_turn_on_room_rule_needs_admin(api):
    view = _as(api, PRIYA, admin=False).post(BASE, json=RULE).json()
    r = _as(api, PRIYA, admin=False).post(f"{BASE}/{view['id']}/activate")
    assert r.status_code == 403
    r = _as(api, SAM).post(f"{BASE}/{view['id']}/activate")
    assert r.status_code == 200 and (r.json()["status"], r.json()["mode"]) == ("active", "observe")
    row = await repo.get_schedule(WS, view["id"])
    assert ("upsert", view["id"], "active") not in api.calls  # no cron: a rule has no Temporal Schedule
    assert row["mode"] == "observe"


async def test_turn_on_calendar_rule_owner_only(api):
    view = _as(api, PRIYA, admin=False).post(BASE, json=CAL).json()
    assert _as(api, SAM).post(f"{BASE}/{view['id']}/activate").status_code == 404  # an admin does not even see it
    r = _as(api, PRIYA, admin=False).post(f"{BASE}/{view['id']}/activate")
    assert r.status_code == 200 and r.json()["mode"] == "observe"
    assert [v["id"] for v in _as(api, SAM).get(BASE).json()["automations"]] == []


async def test_room_rule_cap(api):
    ids = [_as(api, PRIYA, admin=False).post(BASE, json={**RULE, "name": f"r{i}"}).json()["id"] for i in range(6)]
    for sid in ids[:5]:
        assert _as(api).post(f"{BASE}/{sid}/activate").status_code == 200
    r = _as(api).post(f"{BASE}/{ids[5]}/activate")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "room_rule_cap"
    r = _as(api, PRIYA, admin=False).post(BASE, json={**RULE, "name": "r7"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "room_rule_cap"


async def test_text_edit_drops_to_observe_and_resets_count(api):
    view = _as(api).post(BASE, json=RULE).json()
    sid = view["id"]
    _as(api).post(f"{BASE}/{sid}/activate")
    row = await repo.get_schedule(WS, sid)
    matches = [
        {"at": f"2026-11-04T10:0{i}:00Z", "threadKey": f"room:{ROOM}:m{i}", "wouldDo": f"Would x {i}", "version": 1}
        for i in range(5)
    ]
    row = await repo.update_schedule(
        WS, sid, {"mode": "auto", "stats": {"matches": matches}}, expected_version=row["version"]
    )
    r = _as(api).patch(
        f"{BASE}/{sid}", json={"instructions": "When someone reports a crash here, fix it", "version": row["version"]}
    )
    assert r.status_code == 200, r.text
    view = r.json()
    assert (view["ruleVersion"], view["mode"]) == (2, "observe")
    assert view["stats"] == {"matches": 5, "matchesThisVersion": 0, "doneByHuman": 0}
    listed = _as(api).get(f"/api/v1/automations/{sid}/matches").json()
    assert len(listed["matches"]) == 5 and listed["stats"]["matchesThisVersion"] == 0  # old matches stay listed
    r = _as(api).patch(
        f"{BASE}/{sid}", json={"dailyCapUsd": 5, "version": view["version"]}
    )  # a budget edit keeps the version
    assert (r.json()["ruleVersion"], r.json()["mode"], r.json()["dailyCapUsd"]) == (2, "observe", 5.0)
    r = _as(api).patch(
        f"{BASE}/{sid}", json={"match": {"sources": ["room"], "roomIds": [ROOM, "r2"]}, "version": r.json()["version"]}
    )
    assert r.json()["ruleVersion"] == 3 and r.json()["match"]["roomIds"] == [ROOM, "r2"]


async def test_propose_accepts_match(internal):
    tid = await _chat_trigger()
    body = {**RULE, "triggerIds": [tid]}  # no cron: a rule fires on its matches
    r = internal.api.client.post(PROPOSE, json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    row = await repo.get_schedule(WS, r.json()["automationId"])
    assert (row["status"], row["mode"], row["ruleVersion"], row["cron"], row["ownerUserId"]) == (
        "draft",
        "observe",
        1,
        [],
        SAM,
    )
    assert (row["match"]["sources"], row["match"]["roomIds"], row["createdBy"]) == (["room"], [ROOM], "router")
    assert internal.post.await_args.kwargs["kind"] == "automation_confirm"
    with _not_member(SAM):
        r = internal.api.client.post(PROPOSE, json={**body, "name": "other"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "room_not_member"
    assert len(await repo.list_schedules(WS)) == 1


async def test_propose_needs_a_schedule_or_a_match(internal):
    tid = await _chat_trigger()
    r = internal.api.client.post(PROPOSE, json={"name": "n", "instructions": "i", "triggerIds": [tid]}, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "no_schedule"
    assert await repo.list_schedules(WS) == []


async def test_only_activate_turns_a_rule_on(internal, monkeypatch):
    """Typed text never activates (C20): only the activate route changes a rule's status."""
    tid = await _chat_trigger()
    sid = internal.api.client.post(PROPOSE, json={**RULE, "triggerIds": [tid]}, headers=AUTH).json()["automationId"]
    activate = AsyncMock()
    monkeypatch.setattr("routers.automations._turn_on_rule", activate)
    assert (await repo.get_schedule(WS, sid))["status"] == "draft"
    activate.assert_not_awaited()


async def test_rule_row_never_reaches_the_fire_path(api, internal):
    """A rule has no schedule, no webhook and no Run now (lead, 5 Nov): 409 `rule_never_runs` everywhere."""
    sched = {"cron": ["0 9 * * 1"], "tz": "UTC"}
    r = _as(api).post(BASE, json={**RULE, "schedule": sched})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
    r = _as(api).post(BASE, json={**RULE, "events": [{"provider": "stripe", "eventIds": ["invoice.payment_failed"]}]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
    assert await repo.list_schedules(WS) == []
    view = _as(api).post(BASE, json=RULE).json()
    r = _as(api).patch(f"{BASE}/{view['id']}", json={"schedule": sched, "version": view["version"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
    _as(api).post(f"{BASE}/{view['id']}/activate")
    r = _as(api).post(f"{BASE}/{view['id']}/run")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
    api.temporal.start_workflow.assert_not_awaited()
    # An automation never becomes a rule by PATCH: its Schedule and hooks would stay live behind a rule row.
    plain = _as(api).post(BASE, json={"name": "n", "instructions": "i", "schedule": sched}).json()
    r = _as(api).patch(f"{BASE}/{plain['id']}", json={"match": RULE["match"], "version": plain["version"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
    assert (await repo.get_schedule(WS, plain["id"])).get("match") is None
    tid = await _chat_trigger()
    r = internal.api.client.post(PROPOSE, json={**RULE, "cron": ["0 9 * * 1"], "triggerIds": [tid]}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "rule_never_runs"
