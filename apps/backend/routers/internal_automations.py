"""In-runtime automation routes (hook token): the Router's `propose_automation` lands here (lane A, Task 6;
contracts-v2 §11) and the voice agent's `start_work` (Task 10; §14.1). A proposal is a `draft` row plus a paused Temporal Schedule plus one confirm card in the
room; nothing runs until a person calls `POST /api/v1/automations/{id}/activate` (UX §2, C20)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from temporalio.service import RPCError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from ulid import ULID

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.repositories import automations_repo, messaging_repo, tasks_repo, user_repo
from core.services import admission, automation_schedule, super_agent_flags, task_conversation
from core.temporal import client as temporal
from core.temporal.watches import (
    MAX_EVERY_MIN,
    MAX_GIVE_UP_DAYS,
    MIN_EVERY_MIN,
    WatchSpec,
    give_up_default,
    watch_id_for,
)
from core.services.container_auth import _require_hook_token
from routers.automations import check_deliver_room, check_dm_owner
from routers.rules import RuleBuiltin, RuleMatchIn, check_scope

router = APIRouter()
logger = logging.getLogger(__name__)

# The card renders when, zone, next run and budget, and a Turn on button whenever the automation is off and the
# viewer may turn it on (AutomationConfirmCard), so the line names that button.
CONFIRM_TEXT = "Set up below. Turn it on with the button on the card."


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
    # A cron that pins one date is a one-off; "yearly" keeps it every year on that date.
    repeat: Literal["yearly"] | None = None

    @field_validator("deliver_to", mode="before")
    @classmethod
    def _room_forms(cls, v):
        """ "here" is the default (the trigger's room); a bare "#general" is `room:#general` (dev walk R2)."""
        if isinstance(v, str) and v.strip().lower() in ("here", "room:here"):
            return None
        return f"room:{v}" if isinstance(v, str) and v.startswith("#") else v


# A batch trigger id the model names without its prefix ("fdab1932-…" for `chat:fdab1932-…`, dev walk R2, 5 Nov:
# the proposal 400'd `no_room`). Decisions forgive the same slip (router_prompt._resolve); these are the sources
# a person asks from.
_BARE_SOURCES = ("chat", "text", "mail")


async def _trigger(ws: str, trigger_id: str) -> dict | None:
    trig = await tasks_repo.get_trigger(ws, trigger_id)
    if trig is None and ":" not in trigger_id:
        for source in _BARE_SOURCES:
            if trig := await tasks_repo.get_trigger(ws, ts.trigger_id(source, trigger_id)):
                break
    return trig


def _err(http_status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, **extra})


async def _deliver_room_id(ws: str, requester: str, key: str, here: str) -> str:
    """The id of the room a proposed automation delivers to. The model may name a room ("room:acc-a1-e3ffa8",
    gate run 3) instead of giving its id: a name of exactly one room the requester sits in resolves to its id;
    anything else is 400 `room_not_found`, as the create/patch routes answer (a run posting to a room that is
    not one failed its task's end_attempt)."""
    if key == here:
        return here
    if not key.startswith("#"):  # "#general" is always a name
        try:
            await check_deliver_room(f"room:{key}", ws, requester)
            return key
        except HTTPException:
            pass
    name = key.removeprefix("#").strip().lower()
    rooms = await messaging_repo.list_rooms_for_member(ws, "human", requester)
    ids = {r["meta"]["roomId"] for r in rooms if str(r["meta"].get("name") or "").strip().lower() == name}
    if len(ids) == 1:
        (room_id,) = ids
        await check_deliver_room(f"room:{room_id}", ws, requester)  # discovery only: recheck the seat (400)
        return room_id
    raise _err(400, "room_not_found")


@router.post("/internal/automations/{owner_id}/propose")
async def propose(owner_id: str, body: ProposeIn, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    room_id, requester, thread_root = body.room_id, body.requester_user_id, body.thread_root
    triggers = [t for t in [await _trigger(owner_id, tid) for tid in body.trigger_ids] if t]
    for trig in triggers:
        ref = trig.get("ref") or {}
        room_id = room_id or ref.get("roomId")
        requester = requester or trig.get("actorUserId")
        thread_root = thread_root or ref.get("threadRoot")
    if not room_id:
        raise _err(400, "no_room")
    if not requester:
        raise _err(400, "no_requester")
    check_dm_owner(body.deliver_to, requester)
    deliver_to = body.deliver_to or f"room:{room_id}"
    if deliver_to.startswith("room:"):
        deliver_to = f"room:{await _deliver_room_id(owner_id, requester, deliver_to.removeprefix('room:'), room_id)}"
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
            "remainingRuns": 0 if body.repeat else automation_schedule.remaining_runs(body.cron, None),
            "scheduleSetAt": datetime.now(timezone.utc).isoformat(),
            "policyText": body.instructions.strip(),
            "specialist": body.specialist_id,
            "budgetPerRunUsd": body.budget_per_run_usd,
            "deliverTo": deliver_to,
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
    for trig in triggers:  # the card is this ask's answer: the Router posts no second line for it (R7)
        routed = {**(trig.get("routedTo") or {}), "automationId": sid}
        await tasks_repo.set_trigger_routed_to(owner_id, trig["triggerId"], routed)
    return {"automationId": sid}


# --- voice: work asked aloud becomes a task (Task 10) --------------------------------------------------
# The requester and the room come from the VOICERUN row the backend wrote when it dispatched the turn (the
# Mac tool's authority), never from the model. One utterance = one run id = one trigger: a retried tool call
# is the same task.


class VoiceStartIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)  # the voice request's run = the utterance
    call_id: str = Field(min_length=1, max_length=128)
    room_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20000)
    specialist: str | None = None


@router.post("/internal/automations/{owner_id}/voice-start", status_code=202)
async def voice_start(owner_id: str, body: VoiceStartIn, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    run = await messaging_repo.get_voice_run(owner_id, body.run_id)
    if run is None or run.get("roomId") != body.room_id:
        raise _err(404, "unknown_run")
    requester = run.get("requesterUserId")
    if not requester:
        raise _err(403, "no_requester")  # a guest on a call starts nothing
    meta = await messaging_repo.get_room(body.room_id) or {}
    lane = ts.router_lane_key(requester) if meta.get("kind") == "dm" else "shared"
    trigger_id = ts.trigger_id("voice", body.call_id, body.run_id)
    task_id = ts.router_task_id(trigger_id, 0, 0)
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=body.title.strip(),
        brief=body.brief.strip(),
        specialist=body.specialist,
        ack=f"On it: {body.title.strip()}",
        visibility="room",
    )
    now = utc_now_iso()
    trigger = ts.Trigger(
        ws=owner_id,
        trigger_id=trigger_id,
        source="voice",
        admission="respond",
        lane=lane,
        actor_user_id=requester,
        received_at=now,
        created_at=now,
        updated_at=now,
        ref={
            "deliverRoomId": body.room_id,
            "callId": body.call_id,
            "utteranceId": body.run_id,
            "agentId": run.get("agentId"),
            "taskId": task_id,
            "start": start.model_dump(by_alias=True),
        },
    )
    await admission.enqueue_trigger(owner_id, trigger, room_id=body.room_id)
    return {"triggerId": trigger_id, "taskId": task_id}


# --- watches: a Worker asks its task to watch something that finishes later (Task 12) --------------------------
# The caller proves it is this task's runtime (attempt + runSecret, as the questions route does); the watch id is
# deterministic, so a retried tool call is the same watch. Watches live in the workflow, not in rows.


class WatchIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    run_id: str
    attempt: int = Field(ge=1)
    run_secret: str = Field(min_length=1, max_length=128)
    kind: Literal["pr_checks", "url"]
    target: dict = Field(default_factory=dict)
    every_min: int = Field(default=30, ge=MIN_EVERY_MIN, le=MAX_EVERY_MIN)
    give_up_at: str | None = None  # ISO 8601 UTC; default one day
    on_give_up: Literal["ask", "fail"] = "ask"


class UnwatchIn(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    run_id: str
    attempt: int = Field(ge=1)
    run_secret: str = Field(min_length=1, max_length=128)
    watch_id: str = Field(min_length=1, max_length=64)


def _target_ok(kind: str, target: dict) -> bool:
    if kind == "url":
        url = str(target.get("url") or "")
        return url.startswith(("https://", "http://")) and len(url) <= 2048 and set(target) <= {"url"}
    repo, number = str(target.get("repo") or ""), target.get("number")
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)) and isinstance(number, int) and number > 0


async def _this_tasks_runtime(owner_id: str, task_id: str, attempt: int, run_id: str, run_secret: str) -> None:
    """O's checks, as the questions route does them: the attempt this runtime speaks for (403), and that it
    is the task's current live attempt (409: a later attempt never speaks for an earlier one)."""
    from routers.runtime_internal import _attempt_of, _current_task

    att = await _attempt_of(owner_id, task_id, attempt, run_id, run_secret)
    await _current_task(owner_id, att, task_id, attempt)


@router.post("/internal/tasks/{owner_id}/{task_id}/watches", status_code=202)
async def add_watch(owner_id: str, task_id: str, body: WatchIn, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    await _this_tasks_runtime(owner_id, task_id, body.attempt, body.run_id, body.run_secret)
    if not _target_ok(body.kind, body.target):
        raise _err(400, "invalid_target")
    give_up_at = body.give_up_at or give_up_default()
    try:
        at = datetime.fromisoformat(give_up_at)
    except ValueError as exc:
        raise _err(400, "invalid_give_up_at") from exc
    if at.tzinfo is None:  # a naive time cannot be compared with the workflow's clock
        raise _err(400, "invalid_give_up_at", message="giveUpAt must carry a UTC offset")
    if at > datetime.now(timezone.utc) + timedelta(days=MAX_GIVE_UP_DAYS):
        raise _err(400, "give_up_too_far", message=f"a watch gives up within {MAX_GIVE_UP_DAYS} days")
    give_up_at = at.astimezone(timezone.utc).isoformat()  # one offset, so the string max() of watchUntil is right
    spec = WatchSpec(
        watch_id=watch_id_for(task_id, body.kind, body.target),
        kind=body.kind,
        target=body.target,
        every_min=body.every_min,
        give_up_at=give_up_at,
        on_give_up=body.on_give_up,
    )
    try:
        await temporal.update_task(owner_id, task_id, "add_watch", spec)
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise _err(503, "temporal_unavailable") from exc
    return {"watchId": spec.watch_id, "giveUpAt": give_up_at}


@router.post("/internal/tasks/{owner_id}/{task_id}/watches/remove", status_code=204)
async def remove_watch(owner_id: str, task_id: str, body: UnwatchIn, request: Request) -> Response:
    await _require_hook_token(owner_id, request)
    if not await super_agent_flags.flags_for(owner_id):
        raise _err(404, "not_found")
    await _this_tasks_runtime(owner_id, task_id, body.attempt, body.run_id, body.run_secret)
    try:
        await temporal.task_handle(owner_id, task_id).signal("unwatch", body.watch_id)
    except (temporal.TemporalUnavailable, RPCError) as exc:
        raise _err(503, "temporal_unavailable") from exc
    return Response(status_code=204)
