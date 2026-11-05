"""In-runtime automation routes (hook token): the Router's `propose_automation` lands here (lane A, Task 6;
contracts-v2 §11). A proposal is a `draft` row plus a paused Temporal Schedule plus one confirm card in the
room; nothing runs until a person calls `POST /api/v1/automations/{id}/activate` (UX §2, C20)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from ulid import ULID

from core.repositories import automations_repo, tasks_repo, user_repo
from core.services import automation_schedule, super_agent_flags, task_conversation
from core.services.container_auth import _require_hook_token
from routers.rules import RuleBuiltin, RuleMatchIn, check_scope

router = APIRouter()
logger = logging.getLogger(__name__)

CONFIRM_TEXT = "Set up below. Confirm on the card."  # the card itself renders when, zone, next runs and budget


class ProposeIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=20000)
    cron: list[str] = Field(default=[], max_length=4)  # empty for a rule (§18), which fires on its matches
    tz: str | None = None
    specialist_id: str | None = None
    deliver_to: str | None = Field(default=None, pattern=r"^(none|room:\S+|dm:\S+)$")
    budget_per_run_usd: float = Field(default=1.0, gt=0, le=100)
    # The batch's trigger ids: the room, requester and thread come from their rows unless given.
    trigger_ids: list[str] = []
    requester_user_id: str | None = None
    room_id: str | None = None
    thread_root: str | None = None
    # §18: a proactive rule; validated like the create route, always observe, never offered Auto on the card.
    match: RuleMatchIn | None = None
    speculative: bool = False
    time_limit_s: int | None = Field(default=None, ge=60, le=7200)
    daily_cap_usd: float = Field(default=10.0, gt=0, le=100)
    builtin: RuleBuiltin | None = None


def _err(http_status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, **extra})


@router.post("/internal/automations/{owner_id}/propose")
async def propose(owner_id: str, body: ProposeIn, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    room_id, requester, thread_root = body.room_id, body.requester_user_id, body.thread_root
    for trigger_id in body.trigger_ids:
        trig = await tasks_repo.get_trigger(owner_id, trigger_id)
        if trig is None:
            continue
        ref = trig.get("ref") or {}
        room_id = room_id or ref.get("roomId")
        requester = requester or trig.get("actorUserId")
        thread_root = thread_root or ref.get("threadRoot")
    if not room_id:
        raise _err(400, "no_room")
    if not requester:
        raise _err(400, "no_requester")
    if not body.cron and body.match is None:
        raise _err(400, "no_schedule")
    if body.cron and body.match is not None:
        raise _err(409, "rule_never_runs")  # a rule fires on its matches, never on a schedule
    tz = body.tz or await user_repo.get_timezone(requester)
    if body.cron:
        try:
            automation_schedule.validate_cron(body.cron, tz)
        except ValueError as exc:
            raise _err(400, "invalid_schedule", message=str(exc)) from exc
    rule: dict = {}
    if body.match is not None:
        rule = {
            "match": await check_scope(owner_id, requester, body.match, body.builtin),
            "mode": "observe",
            "speculative": body.speculative,
            "timeLimitS": body.time_limit_s,
            "dailyCapUsd": body.daily_cap_usd,
            "builtin": body.builtin,
            "ruleVersion": 1,
        }
    row, _ = await automations_repo.create_schedule(
        owner_id,
        {
            "scheduleId": str(ULID()),
            "name": body.name.strip(),
            "ownerUserId": requester,
            "cron": body.cron,
            "tz": tz,
            "policyText": body.instructions.strip(),
            "specialist": body.specialist_id,
            "budgetPerRunUsd": body.budget_per_run_usd,
            "deliverTo": body.deliver_to or f"room:{room_id}",
            "createdBy": "router",
            **rule,
        },
    )
    sid = row["scheduleId"]
    try:
        await automation_schedule.upsert(row)  # paused: the card shows the next runs of a draft (S-07)
        await task_conversation.post_as_lightbulb(
            owner_id,
            room_id,
            text=CONFIRM_TEXT,
            event_id=f"automation_confirm:{sid}",
            kind="automation_confirm",
            thread_root=thread_root,
            attrs={"automationId": sid},
        )
    except Exception as exc:  # noqa: BLE001 — a draft nobody can see is an orphan: take it back out
        logger.warning("propose: card not posted owner=%s automation=%s", owner_id, sid, exc_info=True)
        await automation_schedule.delete(owner_id, sid)
        await automations_repo.delete_schedule(owner_id, sid)
        raise _err(502, "card_not_posted") from exc
    return {"automationId": sid}
