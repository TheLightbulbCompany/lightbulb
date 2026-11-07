"""Proactive rule routes (contracts §18, spec 12 §3.4; wave2-S Task 5): a rule's mode and its observe record.

`POST /api/v1/automations/{id}/mode` and `GET /api/v1/automations/{id}/matches`. Both 404 for rows without a
`match` scope (ordinary automations). Who may change a mode (founder, 4 Nov;
lead, 5 Nov): only its owner turns a personal (calendar) rule on, and only the owner reads its record; an org admin
may still turn it off. Room rules need an org admin to turn on; any audience member may drop an auto rule back to
observe ("Quiet this"); auto needs an admin, 5 matches on this version and billing, and is refused in wave 2
(`rules.AUTO_AVAILABLE`). Creating, editing and activating rules ride lane A's
automation routes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from core import tasks_schema as ts
from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import automations_repo, messaging_repo
from core.repositories.tasks_repo import VersionConflict
from core.services import chat_gate, rules

router = APIRouter()


class RuleMatchIn(BaseModel):
    """`match` on create / patch / propose (spec 12 §3.1): one source, its rooms or its account."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    sources: list[Literal["room", "calendar"]] = Field(min_length=1)
    room_ids: list[str] = []
    account: str | None = None
    repo: str | None = None


RuleBuiltin = Literal["invite_prep", "repro_fix"]


async def check_scope(
    ws: str, owner_user_id: str, match: RuleMatchIn, builtin: str | None, *, except_sid: str | None = None
) -> dict:
    """spec 12 §3.2: the owner is in every room, at most `RULES_PER_ROOM` active rules per room, one source,
    a repo for repro-fix. Returns the scope as stored; raises 409 with the code otherwise."""
    scope = match.model_dump(by_alias=True, exclude_none=True)
    member = {r for r in match.room_ids if await messaging_repo.get_member(r, "human", owner_user_id) is not None}
    counts: dict[str, int] = {}
    for rule in await automations_repo.list_rules(ws):
        if rule.get("status") == "active" and rule["scheduleId"] != except_sid:
            for room_id in (rule.get("match") or {}).get("roomIds") or []:
                counts[room_id] = counts.get(room_id, 0) + 1
    code = rules.validate_scope(scope, member_room_ids=member, active_rules_by_room=counts, builtin=builtin)
    if code:
        raise _err(409, code)
    return scope


def rule_stats(row: dict) -> dict:
    """The observe numbers on a rule's view: all matches kept, those on this version, those a person did."""
    matches = (row.get("stats") or {}).get("matches") or []
    version = row.get("ruleVersion", 1)
    current = [m for m in matches if m.get("version") == version]
    return {
        "matches": len(matches),
        "matchesThisVersion": len(current),
        "doneByHuman": sum(1 for m in current if m.get("doneByHuman")),
    }


class ModeIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: ts.RuleMode
    expect_version: int = Field(alias="expectVersion")


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


async def _rule(auth: AuthContext, sid: str) -> tuple[str, dict]:
    ws = resolve_owner_id(auth)
    row = await automations_repo.get_schedule(ws, sid)
    if not row or not row.get("match"):
        raise _err(404, "not_found")
    return ws, row


def _calendar(row: dict) -> bool:
    return "calendar" in row["match"].get("sources", [])


def _owner(row: dict, auth: AuthContext) -> bool:
    return row.get("ownerUserId") == auth.user_id


async def _in_audience(row: dict, auth: AuthContext) -> bool:
    """Who sees a rule's record and may quiet it. A personal (calendar) rule's audience is its owner alone
    (lead ruling, 5 Nov); a room rule's is its owner, the org admins and the members of its rooms."""
    if _calendar(row):
        return _owner(row, auth)
    if auth.is_org_admin or _owner(row, auth):
        return True
    for room_id in row["match"].get("roomIds") or []:
        if await messaging_repo.get_member(room_id, "human", auth.user_id) is not None:
            return True
    return False


@router.post("/automations/{sid}/mode")
async def set_mode(sid: str, body: ModeIn, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws, row = await _rule(auth, sid)
    if body.mode == "auto":
        require_org_admin(auth)
        if rules.matches_on_version(row) < rules.MIN_MATCHES_TO_PROMOTE:
            raise _err(409, "not_enough_matches")
        gate = await chat_gate.gate_chat(user_id=auth.user_id, owner_id=ws, session_key=f"rule:{sid}")
        if gate.get("blocked"):
            raise HTTPException(status_code=402, detail={"code": gate.get("code"), "message": gate.get("message")})
        if not rules.AUTO_AVAILABLE:
            raise _err(409, "not_available")
    elif body.mode == "off":  # owner or org admin, personal rules included
        if not (auth.is_org_admin or _owner(row, auth)):
            raise _err(403, "forbidden")
    elif row.get("mode") == "auto":  # demote: "Quiet this"
        if not await _in_audience(row, auth):
            raise _err(403, "forbidden")
    elif _calendar(row):  # turning a personal rule on: its owner only, not an admin
        if not _owner(row, auth):
            raise _err(403, "forbidden")
    else:  # turning a room rule on
        require_org_admin(auth)
    try:
        return await automations_repo.update_schedule(
            ws, sid, {"mode": body.mode}, expected_version=body.expect_version
        )
    except VersionConflict:
        raise _err(409, "stale_version") from None


@router.get("/automations/{sid}/matches")
async def list_matches(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """The observe record, newest first. ponytail: the row keeps at most 50 entries, so no cursor yet."""
    _, row = await _rule(auth, sid)
    if not await _in_audience(row, auth):
        raise _err(403, "forbidden")
    matches = (row.get("stats") or {}).get("matches") or []
    current = [m for m in matches if m.get("version") == row.get("ruleVersion", 1)]
    return {
        "matches": [
            {"at": m["at"], "wouldDo": m["wouldDo"], "doneByHuman": bool(m.get("doneByHuman")), "version": m["version"]}
            for m in reversed(matches)
        ],
        "stats": {
            "matchesThisVersion": len(current),
            "doneByHuman": sum(1 for m in current if m.get("doneByHuman")),
        },
    }
