"""Proactive rule routes (contracts §18, spec 12 §3.4; wave2-S Task 5): a rule's mode and its observe record.

`POST /api/v1/automations/{id}/mode` and `GET /api/v1/automations/{id}/matches`. Both 404 unless the workspace's
switch is on, and for rows without a `match` scope (ordinary automations). Who may change a mode (founder, 4 Nov):
a person turns their own personal (calendar) rule on or off; room rules need an org admin to turn on; any audience
member may drop an auto rule back to observe ("Quiet this"); auto needs an admin, 5 matches on this version and
billing, and is refused in wave 2 (`rules.AUTO_AVAILABLE`). Creating, editing and activating rules ride lane A's
automation routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core import tasks_schema as ts
from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import automations_repo, messaging_repo
from core.repositories.tasks_repo import VersionConflict
from core.services import chat_gate, rules, super_agent_flags

router = APIRouter()


class ModeIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: ts.RuleMode
    expect_version: int = Field(alias="expectVersion")


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


async def _rule(auth: AuthContext, sid: str) -> tuple[str, dict]:
    ws = resolve_owner_id(auth)
    if not await super_agent_flags.flags_for(ws):
        raise _err(404, "not_found")
    row = await automations_repo.get_schedule(ws, sid)
    if not row or not row.get("match"):
        raise _err(404, "not_found")
    return ws, row


def _personal(row: dict, auth: AuthContext) -> bool:
    """The caller's own calendar rule. Room rules are never personal."""
    return "calendar" in row["match"].get("sources", []) and row.get("ownerUserId") == auth.user_id


async def _in_audience(row: dict, auth: AuthContext) -> bool:
    if auth.is_org_admin or row.get("ownerUserId") == auth.user_id:
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
    elif body.mode == "off":
        if not (auth.is_org_admin or row.get("ownerUserId") == auth.user_id):
            raise _err(403, "forbidden")
    elif row.get("mode") == "auto":  # demote: "Quiet this"
        if not await _in_audience(row, auth):
            raise _err(403, "forbidden")
    elif not _personal(row, auth):  # turning a room rule on
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
