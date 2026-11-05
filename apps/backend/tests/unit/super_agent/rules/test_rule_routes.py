"""Rule mode and observe-record routes (wave2-S Task 5, spec 12 §3.4, contracts §18)."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from core.auth import AuthContext, get_current_user
from core.repositories import automations_repo
from core.services import chat_gate, super_agent_flags
from routers import rules as router_mod

pytestmark = pytest.mark.asyncio
WS = "org_1"
SAM, PRIYA, EVE = "user_sam", "user_priya", "user_eve"  # Sam admin; Priya member of #dev; Eve not in #dev


@pytest.fixture
def world(table, monkeypatch):
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=True))
    gate = AsyncMock(return_value={"blocked": False})
    monkeypatch.setattr(chat_gate, "gate_chat", gate)
    members = {("dev", PRIYA), ("dev", SAM)}
    monkeypatch.setattr(
        router_mod.messaging_repo,
        "get_member",
        AsyncMock(side_effect=lambda room, kind, uid: {"id": uid} if (room, uid) in members else None),
    )
    who = {"user": SAM}
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id=who["user"], org_id=WS, org_role="org:admin" if who["user"] == SAM else "org:member"
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return {"client": client, "who": who, "gate": gate}


async def _rule(sid="s1", *, owner=SAM, mode="off", status="active", match=None, matches=0, rule_version=1):
    row, _ = await automations_repo.create_schedule(
        WS,
        {
            "scheduleId": sid,
            "tz": "UTC",
            "policyText": "When someone reports a bug here, reproduce it",
            "name": "reproduce the bug",
            "ownerUserId": owner,
            "match": match or {"sources": ["room"], "roomIds": ["dev"]},
            "mode": mode,
            "ruleVersion": rule_version,
            "stats": {
                "matches": [
                    {"at": f"2026-11-04T10:0{i}:00Z", "threadKey": f"room:dev:m{i}", "wouldDo": f"Would x {i}",
                     "version": 1}
                    for i in range(matches)
                ]
            },
        },
    )  # fmt: skip
    if status != "draft":
        row = await automations_repo.set_status(WS, sid, status, reason=None, allowed_from=frozenset({"draft"}))
    return row


async def _mode(world, user, mode, row, sid="s1"):
    world["who"]["user"] = user
    return await world["client"].post(
        f"/api/v1/automations/{sid}/mode", json={"mode": mode, "expectVersion": row["version"]}
    )


async def test_turn_on_room_rule_needs_admin(world):
    row = await _rule()
    r = await _mode(world, PRIYA, "observe", row)
    assert r.status_code == 403
    r = await _mode(world, SAM, "observe", row)
    assert r.status_code == 200 and r.json()["mode"] == "observe"


async def test_personal_rule_owner_only(world):
    """A calendar rule: only its owner turns it on and reads its record; an admin may still turn it off."""
    cal = await _rule("cal", owner=PRIYA, match={"sources": ["calendar"], "account": "priya@x.com"}, matches=1)
    assert (await _mode(world, SAM, "observe", cal, sid="cal")).status_code == 403  # admin, not the owner
    r = await _mode(world, PRIYA, "observe", cal, sid="cal")
    assert r.status_code == 200 and r.json()["mode"] == "observe"
    world["who"]["user"] = SAM
    assert (await world["client"].get("/api/v1/automations/cal/matches")).status_code == 403
    world["who"]["user"] = PRIYA
    assert (await world["client"].get("/api/v1/automations/cal/matches")).json()["stats"]["matchesThisVersion"] == 1
    r = await _mode(world, SAM, "off", r.json(), sid="cal")
    assert r.status_code == 200 and r.json()["mode"] == "off"


async def test_promote_refused_in_wave2(world):
    row = await _rule(mode="observe", matches=4)
    assert (await _mode(world, PRIYA, "auto", row)).status_code == 403
    r = await _mode(world, SAM, "auto", row)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_enough_matches"
    row5 = await _rule("s5", mode="observe", matches=5)
    r = await _mode(world, SAM, "auto", row5, sid="s5")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_available"
    world["gate"].assert_awaited_once()
    assert (await automations_repo.get_schedule(WS, "s5"))["mode"] == "observe"


async def test_promote_blocked_by_billing(world):
    row = await _rule(mode="observe", matches=5)
    world["gate"].return_value = {"blocked": True, "code": "out_of_credits", "message": "Add credits"}
    r = await _mode(world, SAM, "auto", row)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "out_of_credits"


async def test_edit_count_uses_rule_version(world):
    """5 matches on ruleVersion 1, then an edit (ruleVersion 2): promotion counts zero again."""
    row = await _rule(mode="observe", matches=5, rule_version=2)
    r = await _mode(world, SAM, "auto", row)
    assert r.json()["detail"]["code"] == "not_enough_matches"
    world["who"]["user"] = PRIYA
    body = (await world["client"].get("/api/v1/automations/s1/matches")).json()
    assert body["stats"]["matchesThisVersion"] == 0 and len(body["matches"]) == 5


async def test_off_and_quiet(world):
    row = await _rule(mode="auto", owner=SAM)
    r = await _mode(world, EVE, "observe", row)
    assert r.status_code == 403  # not in the rule's room
    r = await _mode(world, PRIYA, "observe", row)
    assert r.status_code == 200  # any audience member may quiet an auto rule
    r = await _mode(world, PRIYA, "off", r.json())
    assert r.status_code == 403  # off: owner or admin
    row = await automations_repo.get_schedule(WS, "s1")
    assert (await _mode(world, SAM, "off", row)).json()["mode"] == "off"


async def test_stale_version_409(world):
    row = await _rule()
    r = await _mode(world, SAM, "observe", {**row, "version": row["version"] - 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version"


async def test_matches_audience_only(world):
    await _rule(mode="observe", matches=2)
    world["who"]["user"] = EVE
    assert (await world["client"].get("/api/v1/automations/s1/matches")).status_code == 403
    world["who"]["user"] = PRIYA
    body = (await world["client"].get("/api/v1/automations/s1/matches")).json()
    assert [m["wouldDo"] for m in body["matches"]] == ["Would x 1", "Would x 0"]
    assert body["stats"] == {"matchesThisVersion": 2, "doneByHuman": 0}


async def test_switch_off_and_plain_automation_404(world, monkeypatch):
    await automations_repo.create_schedule(
        WS, {"scheduleId": "plain", "tz": "UTC", "policyText": "x", "name": "x", "ownerUserId": SAM}
    )
    assert (await world["client"].get("/api/v1/automations/plain/matches")).status_code == 404
    await _rule()
    monkeypatch.setattr(super_agent_flags, "flags_for", AsyncMock(return_value=False))
    assert (await world["client"].get("/api/v1/automations/s1/matches")).status_code == 404
