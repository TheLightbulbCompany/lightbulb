"""Shared rule scope helpers for the automation routes (spec 12 §3): the `match` body, its scope check and the
observe numbers on a rule's view. (The mode / matches routes were deleted: nothing called them.)"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from core.repositories import automations_repo, messaging_repo
from core.services import rules


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


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})
