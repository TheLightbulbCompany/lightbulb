"""1-click Automations trigger seeding — "connect Stripe -> pick 'payment
failed' -> get a pre-filled automation."

``POST /automations/from-integration`` composes the existing routine
machinery via ``core/services/inbound_triggers.py``. Gated the same way as
every other trigger-creation path in the system:

- ``require_org_admin`` — wiring an integration trigger is an admin action,
  same posture as every mutating endpoint in ``routers/connections.py``.
- ``require_always_on_entitlement`` — a webhook trigger IS always-on
  automation: a trial container scales to zero and never wakes to serve a triggered dispatch.

``PUT /automations/{public_id}/webhook-secret`` is the guided-paste
companion: once the owner has pasted the hook URL into their Stripe
Dashboard, Stripe hands them a signing secret and the frontend posts it here.
It stores the secret and arms the owner's ``HOOK#`` row (``automation_hooks.arm``);
``routers/hooks.py`` verifies every delivery against it. A foreign or typo'd id 404s.
"""

from __future__ import annotations

import logging

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from temporalio.service import RPCError
from ulid import ULID

from core.temporal import client as temporal_client
from core.temporal.automations import SKIP_TEXTS, FireInput

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import automations_repo, messaging_repo, tasks_repo
from core.repositories.tasks_repo import IllegalTransition, VersionConflict
from core.services.inbound_triggers import (
    seed_trigger_routine,
)
from core.services import automation_hooks, automation_schedule, chat_gate, tasks_access
from core.services.trigger_fire_url import trigger_fire_url
from routers.rules import RuleBuiltin, RuleMatchIn, check_scope, rule_stats
from core.services.entitlement_guards import WEBHOOK_AUTOMATIONS_MESSAGE, require_always_on_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/automations", tags=["automations"])


class FromIntegrationBody(BaseModel):
    provider: str
    # The events this ONE trigger is armed for. A Paperclip webhook trigger has
    # no event filter and every fan-out provider lets one endpoint subscribe to
    # many events, so picking three Stripe events is one URL to paste, not
    # three. All of them must share a registration shape — the seed 400s with
    # the conflicting ids otherwise.
    # 300 comfortably clears the largest catalog in the registry (Stripe's
    # ~265 events) while still bounding the request.
    event_ids: list[str] | None = Field(default=None, min_length=1, max_length=300)
    # Deprecated single-event alias, mapped into `event_ids` below. Kept so
    # callers written against the one-event shape keep working; send one or the
    # other, never both.
    event_id: str | None = Field(default=None, min_length=1)
    # Provider-side resource for auto-registering shapes — the "owner/repo"
    # to hook for auto_github. Guided-paste providers ignore it.
    resource: str | None = None
    # The owner's own Pub/Sub topic ("projects/<proj>/topics/<name>") — only
    # the owner_setup shape (gmail.message) reads it.
    topic: str | None = None
    # Who the seeded routine lands on. Omitted, the seed resolves the owner's
    # Friday as before. Validated against the caller's own Paperclip company
    # roster inside the seed — never trusted from here.
    assignee_agent_id: str | None = None
    # ATTACH instead of create: arm this event on a routine the owner already
    # wrote, leaving its assignee/reviewers/variables/schedule untouched and
    # its instructions intact (the shapes that need maintenance text APPEND a
    # delimited block). Validated against the caller's own company's routines
    # inside the seed — never trusted from here.
    routine_id: str | None = None
    # What the owner typed over the pre-filled draft in the create dialog.
    # The frontend sends these ONLY when the draft was actually edited, so the
    # 1-click path stays byte-identical to the registry template.
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def _exactly_one_event_source(self) -> "FromIntegrationBody":
        if (self.event_ids is None) == (self.event_id is None):
            raise ValueError("send event_ids (preferred) or the deprecated event_id, not both and not neither")
        return self

    def events(self) -> list[str]:
        """The armed event list either spelling resolves to, deduped in the
        order the caller sent them (the first is the primary event)."""
        return list(dict.fromkeys(self.event_ids if self.event_ids is not None else [self.event_id or ""]))


class WebhookSecretBody(BaseModel):
    webhook_secret: str = Field(min_length=1, max_length=512)


@router.post("/from-integration")
async def from_integration(
    body: FromIntegrationBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Arm a webhook-triggered automation from a connected integration's events (a ``HOOK#`` row) —
    or, with ``routine_id``, add them to an automation the owner already has."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=WEBHOOK_AUTOMATIONS_MESSAGE)
    try:
        return await seed_trigger_routine(
            owner_id,
            body.provider,
            body.events(),
            resource=body.resource,
            topic=body.topic,
            assignee_agent_id=body.assignee_agent_id,
            routine_id=body.routine_id,
            title=body.title,
            description=body.description,
            actor_user_id=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{public_id}/webhook-secret")
async def set_webhook_secret(
    public_id: str,
    body: WebhookSecretBody,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Arm one guided-paste trigger with its provider-issued signing secret
    (the paste half of the guided flow — the frontend calls this once the
    owner has the ``whsec_`` Stripe showed them).

    Stores the secret and arms the caller's ``HOOK#`` row for ``public_id`` (404 when there is none).
    """
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)

    # Same path-injection / unbounded-key guard the proxy fire route uses
    # (publicIds are 24-hex upstream).
    if not public_id.isalnum() or len(public_id) > 64:
        raise HTTPException(status_code=400, detail="invalid_public_id")

    # The secret goes to Secrets Manager and the HOOK# row is armed; no HOOK# row is an unknown trigger.
    if not await automation_hooks.arm(owner_id, public_id, body.webhook_secret):
        raise HTTPException(status_code=404, detail="unknown_trigger")
    return {"status": "stored"}


# =====================================================================================================
# Super-agent automations (lane A, Task 6; contracts-v2 §11, spec 06 §5.1). An automation is one SCHED# row (+ at most one Temporal Schedule, kept in
# step by `automation_schedule.upsert`) and its HOOK# rows (`automation_hooks`). Activation happens only
# here (`/activate`); typed text never activates (C20).
# =====================================================================================================

_DELIVER_TO = re.compile(r"^(none|room:\S+|dm:\S+)$")
_ACTIVATE_MESSAGE = "Automations run on the paid plan. Upgrade to turn this one on."


class _In(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ScheduleIn(_In):
    cron: list[str] = Field(min_length=1, max_length=4)  # 5-field, wall clock in `tz`
    tz: str
    remaining_runs: int | None = Field(default=None, ge=0)  # 1 = one-off, 0 = no limit (a dated cron yearly)


class EventTriggerIn(_In):
    provider: str
    event_ids: list[str] = Field(min_length=1)
    resource: str | None = None
    topic: str | None = None


class AutomationIn(_In):
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=20000)
    specialist_id: str | None = None
    deliver_to: str = Field(default="none", pattern=_DELIVER_TO.pattern)
    budget_per_run_usd: float = Field(default=1.0, gt=0, le=100)
    schedule: ScheduleIn | None = None
    events: list[EventTriggerIn] = []
    activate: bool = False
    # §18: with `match` the automation is a proactive rule; it starts in observe.
    match: RuleMatchIn | None = None
    speculative: bool = False
    time_limit_s: int | None = Field(default=None, ge=60, le=7200)
    daily_cap_usd: float = Field(default=10.0, gt=0, le=100)
    builtin: RuleBuiltin | None = None


class AutomationPatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    instructions: str | None = Field(default=None, min_length=1, max_length=20000)
    specialist_id: str | None = None
    deliver_to: str | None = Field(default=None, pattern=_DELIVER_TO.pattern)
    budget_per_run_usd: float | None = Field(default=None, gt=0, le=100)
    schedule: ScheduleIn | None = None
    match: RuleMatchIn | None = None
    speculative: bool | None = None
    time_limit_s: int | None = Field(default=None, ge=60, le=7200)
    daily_cap_usd: float | None = Field(default=None, gt=0, le=100)
    version: int


def _err(http_status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"code": code, **extra})


def check_dm_owner(deliver_to: str | None, owner_user_id: str) -> None:
    """A `dm:` delivery is the automation owner's own DM with Lightbulb; anyone else's DM would show them a
    person-visible run they cannot open. 400 `dm_owner_only` at save time (fire_automation keeps a backstop)."""
    if deliver_to and deliver_to.startswith("dm:") and deliver_to.removeprefix("dm:") != owner_user_id:
        raise _err(400, "dm_owner_only")


async def check_deliver_room(deliver_to: str | None, ws: str, user_id: str) -> None:
    """A `room:` delivery names a room of this workspace the saver sits in. 400 `room_not_found` at save time: a
    run delivering to a room that does not exist failed its whole Router batch (gate run 2, A11)."""
    if not (deliver_to and deliver_to.startswith("room:")):
        return
    from core.services import messaging

    try:
        await messaging.require_room_member(ws, user_id, deliver_to.removeprefix("room:"))
    except messaging.ApiProblem as exc:
        raise _err(400, "room_not_found") from exc


async def _ws(auth: AuthContext) -> str:
    ws = resolve_owner_id(auth)
    return ws


def _may_edit(row: dict, auth: AuthContext) -> None:
    if not (auth.is_org_admin or row.get("ownerUserId") == auth.user_id):
        raise _err(403, "forbidden")


def _may_activate(row: dict, auth: AuthContext) -> None:
    """Turning an automation on: a room automation runs for everyone in the room, so an org admin (the
    Router's proposal is deliverTo=room with ownerUserId=requester); a personal one, its owner only."""
    # A room automation, or one the workspace itself owns (the proactive layer: owner id = the workspace).
    if (row.get("deliverTo") or "none").startswith("room:") or row.get("ownerUserId") == auth.org_id:
        require_org_admin(auth)
    elif row.get("ownerUserId") != auth.user_id:
        raise _err(403, "forbidden")


async def _visible(row: dict, auth: AuthContext) -> bool:
    """Who sees an automation: its owner, an org admin, and the members of the room it delivers to. A rule's
    audience is the rooms it watches; a personal (calendar) rule is its owner's alone (lead, 5 Nov)."""
    match = row.get("match") or {}
    if "calendar" in match.get("sources", []):
        return row.get("ownerUserId") == auth.user_id
    if auth.is_org_admin or row.get("ownerUserId") == auth.user_id:
        return True
    deliver_to = row.get("deliverTo") or "none"
    rooms = [deliver_to.removeprefix("room:")] if deliver_to.startswith("room:") else []
    for room_id in [*rooms, *(match.get("roomIds") or [])]:
        if await messaging_repo.get_member(room_id, "human", auth.user_id) is not None:
            return True
    return False


async def _row(ws: str, sid: str, auth: AuthContext) -> dict:
    row = await automations_repo.get_schedule(ws, sid)
    if row is None or not await _visible(row, auth):
        raise _err(404, "not_found")  # hidden reads as missing, as the task routes do
    return row


def _schedule_fields(schedule: ScheduleIn) -> dict:
    try:
        automation_schedule.validate_cron(schedule.cron, schedule.tz)
    except ValueError as exc:
        raise _err(400, "invalid_schedule", message=str(exc)) from exc
    remaining = automation_schedule.remaining_runs(schedule.cron, schedule.remaining_runs)
    now = datetime.now(timezone.utc).isoformat()  # a one-off's date counts from here (`one_off_passed`)
    return {"cron": schedule.cron, "tz": schedule.tz, "remainingRuns": remaining, "scheduleSetAt": now}


async def _view(ws: str, row: dict) -> dict:
    """`AutomationView` (spec 06 §5.1): times in UTC, the client renders them in `schedule.tz` (C19)."""
    sid = row["scheduleId"]
    hooks = [h for h in [await automations_repo.get_hook(ws, pid) for pid in row.get("hookPublicIds") or []] if h]
    next_runs: list[datetime] = []
    if row.get("cron"):
        try:
            next_runs = await automation_schedule.next_runs(ws, sid)
        except Exception:  # noqa: BLE001 — Temporal down: the card shows no dates rather than failing the view
            logger.warning("automations: next_runs unavailable owner=%s automation=%s", ws, sid, exc_info=True)
    return {
        "id": sid,
        "name": row["name"],
        "instructions": row["policyText"],
        "status": row["status"],
        "pausedReason": row.get("pausedReason"),
        "schedule": (
            {"cron": row["cron"], "tz": row["tz"], "remainingRuns": row.get("remainingRuns")}
            if row.get("cron")
            else None
        ),
        "nextRuns": [t.isoformat() for t in next_runs],
        "hooks": [
            {
                "publicId": h["publicId"],
                "provider": h["provider"],
                "events": h.get("events") or [],
                "fireUrl": trigger_fire_url(ws, h["publicId"]),
                "armed": bool(h.get("armed")),
                "lastSigFailAt": h.get("lastSigFailAt"),
            }
            for h in hooks
        ],
        "specialistId": row.get("specialist"),
        "deliverTo": row.get("deliverTo") or "none",
        "budgetPerRunUsd": row.get("budgetPerRunUsd"),
        "ownerUserId": row["ownerUserId"],
        "createdBy": row.get("createdBy") or "user",
        "mode": row.get("mode"),
        # §18 rule fields; null `match` means an ordinary automation.
        "match": {k: v for k, v in row["match"].items() if v is not None} if row.get("match") else None,
        "speculative": bool(row.get("speculative")),
        "timeLimitS": row.get("timeLimitS"),
        "dailyCapUsd": row.get("dailyCapUsd"),
        "builtin": row.get("builtin"),
        "ruleVersion": row.get("ruleVersion"),
        "promotedBy": row.get("promotedBy"),
        "promotedAt": row.get("promotedAt"),
        "stats": rule_stats(row) if row.get("match") else None,
        "lastTaskId": row.get("lastTaskId"),
        "activeRunTaskId": row.get("activeRunTaskId"),
        "runCount": len(row.get("runTaskIds") or []),
        "version": row["version"],
    }


async def _activate(ws: str, row: dict) -> dict:
    """draft / paused / paused_system → active. A Google watch that broke or lapsed while paused is opened
    again first (and its keeper restarted); when that fails the automation stays paused."""
    if at := automation_schedule.one_off_passed(row, datetime.now(timezone.utc)):
        raise _err(409, "one_off_passed", message=f"This was for {at.day} {at:%b}; set a new time to turn it on.")
    now = datetime.now(timezone.utc).isoformat()
    for pid in row.get("hookPublicIds") or []:
        hook = await automations_repo.get_hook(ws, pid)
        # the broken one (mark_broken disarmed it) or one that lapsed while paused; a healthy watch is left alone
        if hook and hook.get("account") and (not hook.get("armed") or (hook.get("expiresAt") or now) <= now):
            try:
                expires_ms = await automation_hooks.renew(ws, pid)
            except Exception as exc:  # noqa: BLE001 — gog refused: the owner has to reconnect Google
                logger.warning("automations: google watch re-arm failed owner=%s hook=%s", ws, pid, exc_info=True)
                raise _err(409, "google_watch_broken") from exc
            if expires_ms:
                await automation_hooks.start_keeper(ws, pid, expires_ms)
    try:
        row = await automations_repo.set_status(
            ws, row["scheduleId"], "active", reason=None, allowed_from=frozenset({"draft", "paused", "paused_system"})
        )
    except IllegalTransition as exc:
        raise _err(409, "illegal_state", status=row["status"]) from exc
    if row.get("cron"):
        await automation_schedule.upsert(row)
    return row


async def _may_turn_on_rule(ws: str, row: dict, auth: AuthContext) -> None:
    """Who turns a rule on (contracts §18; lead, 5 Nov): a personal (calendar) rule its owner alone, a room rule
    an org admin, within the room cap. 403 / 409 otherwise. Typed text never gets here (C20)."""
    match = row["match"]
    if "calendar" in match.get("sources", []):
        if row.get("ownerUserId") != auth.user_id:
            raise _err(403, "forbidden")
    else:
        require_org_admin(auth)
        await check_scope(
            ws, row["ownerUserId"], RuleMatchIn.model_validate(match), row.get("builtin"), except_sid=row["scheduleId"]
        )


async def _turn_on_rule(ws: str, row: dict, auth: AuthContext) -> dict:
    """A rule turns on in observe, always."""
    await _may_turn_on_rule(ws, row, auth)
    row = await _activate(ws, row)
    if row.get("mode") != "observe":
        row = await automations_repo.update_schedule(
            ws, row["scheduleId"], {"mode": "observe"}, expected_version=row["version"]
        )
    return row


@router.get("")
async def list_automations(auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    rows = [r for r in await automations_repo.list_schedules(ws) if await _visible(r, auth)]
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return {"automations": [await _view(ws, r) for r in rows]}


@router.post("")
async def create_automation(body: AutomationIn, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    check_dm_owner(body.deliver_to, auth.user_id)
    await check_deliver_room(body.deliver_to, ws, auth.user_id)
    if body.events:  # a webhook on a provider is an org-admin act, as `from_integration` is today
        require_org_admin(auth)
    fields = _schedule_fields(body.schedule) if body.schedule else {"cron": [], "tz": "UTC"}
    if body.match is not None and (body.schedule is not None or body.events):
        raise _err(409, "rule_never_runs")  # a rule fires on its matches, never on a schedule or a webhook
    if body.match is not None:  # a rule: any member, for rooms they are in or their own calendar; starts in observe
        fields |= {
            "match": await check_scope(ws, auth.user_id, body.match, body.builtin),
            "mode": "observe",
            "speculative": body.speculative,
            "timeLimitS": body.time_limit_s,
            "dailyCapUsd": body.daily_cap_usd,
            "builtin": body.builtin,
            "ruleVersion": 1,
        }
    row, _ = await automations_repo.create_schedule(
        ws,
        {
            "scheduleId": str(ULID()),
            "name": body.name.strip(),
            "ownerUserId": auth.user_id,
            "policyText": body.instructions.strip(),
            "specialist": body.specialist_id,
            "budgetPerRunUsd": body.budget_per_run_usd,
            "deliverTo": body.deliver_to,
            "createdBy": "user",
            **fields,
        },
    )
    sid = row["scheduleId"]
    setup = []
    for ev in body.events:
        try:
            out = await automation_hooks.seed(
                ws,
                ev.provider,
                ev.event_ids,
                resource=ev.resource,
                topic=ev.topic,
                specialist=None,
                automation_id=sid,
                title=None,
                description=None,
                actor_user_id=auth.user_id,
            )
        except ValueError as exc:
            await _destroy(ws, sid)
            raise _err(400, "invalid_trigger", message=str(exc)) from exc
        setup.append(
            {"publicId": out["trigger_public_id"], "guided": out["guided"], "instructions": out["instructions"]}
        )
    row = await automations_repo.get_schedule(ws, sid) or row
    if row.get("cron"):
        await automation_schedule.upsert(row)  # paused until activated
    if body.activate:
        try:
            if row.get("match"):
                await _may_turn_on_rule(ws, row, auth)
            else:
                _may_activate(row, auth)
            await require_always_on_entitlement(ws, message=_ACTIVATE_MESSAGE)
        except HTTPException:
            await _destroy(ws, sid)  # not turned on: nothing of it is kept
            raise
        row = await (_turn_on_rule(ws, row, auth) if row.get("match") else _activate(ws, row))
    return {**await _view(ws, row), "setup": setup}


@router.get("/{sid}")
async def get_automation(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    return await _view(ws, await _row(ws, sid, auth))


@router.get("/{sid}/runs")
async def list_runs(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """History = the run tasks (newest first), as the viewer may see them. ponytail: the row keeps ≤50 ids."""
    from routers.tasks import _summary  # O's TaskSummary builder (names open answerers)

    ws = await _ws(auth)
    row = await _row(ws, sid, auth)
    ids = [t for t in [row.get("activeRunTaskId"), *(row.get("runTaskIds") or [])] if t]
    runs = []
    for task_id in dict.fromkeys(ids):
        tree = await tasks_repo.get_task_tree(ws, task_id)
        if tree["task"] is not None and await tasks_access.can_view(tree["task"], auth.user_id):
            runs.append(await _summary(tree, auth.user_id))
    skip = row.get("lastSkip")
    text = SKIP_TEXTS.get((skip or {}).get("reason"))
    skipped = {"at": skip["at"], "text": text} if text else None
    return {"runs": runs, "next": None, "skipped": skipped}


@router.patch("/{sid}")
async def patch_automation(sid: str, body: AutomationPatch, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    row = await _row(ws, sid, auth)
    _may_edit(row, auth)
    fields: dict = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.instructions is not None:
        fields["policyText"] = body.instructions.strip()
    if body.specialist_id is not None:
        fields["specialist"] = body.specialist_id
    if body.deliver_to is not None:
        check_dm_owner(body.deliver_to, row["ownerUserId"])
        await check_deliver_room(body.deliver_to, ws, auth.user_id)
        fields["deliverTo"] = body.deliver_to
    if body.budget_per_run_usd is not None:
        fields["budgetPerRunUsd"] = body.budget_per_run_usd
    if body.match is not None and not row.get("match"):
        raise _err(409, "rule_never_runs")  # an automation does not become a rule: its Schedule and hooks would stay
    if body.schedule is not None and row.get("match"):
        raise _err(409, "rule_never_runs")  # a rule row must never reach the fire path
    if body.schedule is not None:
        fields.update(_schedule_fields(body.schedule))
    if body.match is not None:
        fields["match"] = await check_scope(ws, row["ownerUserId"], body.match, row.get("builtin"), except_sid=sid)
    if body.speculative is not None:
        fields["speculative"] = body.speculative
    if body.time_limit_s is not None:
        fields["timeLimitS"] = body.time_limit_s
    if body.daily_cap_usd is not None:
        fields["dailyCapUsd"] = body.daily_cap_usd
    if (row.get("match") or body.match is not None) and {"policyText", "specialist", "match"} & set(fields):
        # §18: an edited rule is a new rule: text, scope or specialist bump its version and drop it to observe.
        # A budget edit keeps the mode.
        fields["ruleVersion"] = int(row.get("ruleVersion") or 0) + 1
        fields["mode"] = "observe"
    try:
        row = await automations_repo.update_schedule(ws, sid, fields, expected_version=body.version)
    except VersionConflict as exc:
        raise _err(409, "stale_version") from exc
    if body.schedule is not None or row.get("cron"):
        await automation_schedule.upsert(row)
    return await _view(ws, row)


@router.post("/{sid}/activate")
@router.post("/{sid}/resume")
async def activate_automation(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    row = await _row(ws, sid, auth)
    if row.get("match"):
        await _may_turn_on_rule(ws, row, auth)
        await require_always_on_entitlement(ws, message=_ACTIVATE_MESSAGE)
        return await _view(ws, await _turn_on_rule(ws, row, auth))
    _may_activate(row, auth)
    await require_always_on_entitlement(ws, message=_ACTIVATE_MESSAGE)
    return await _view(ws, await _activate(ws, row))


@router.post("/{sid}/pause")
async def pause_automation(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    ws = await _ws(auth)
    row = await _row(ws, sid, auth)
    _may_edit(row, auth)
    try:
        row = await automations_repo.set_status(ws, sid, "paused", reason=None, allowed_from=frozenset({"active"}))
    except IllegalTransition as exc:
        raise _err(409, "illegal_state", status=row["status"]) from exc
    if row.get("cron"):
        await automation_schedule.upsert(row)
    return await _view(ws, row)


async def _destroy(ws: str, sid: str) -> None:
    row = await automations_repo.get_schedule(ws, sid)
    for pid in (row or {}).get("hookPublicIds") or []:
        await automation_hooks.delete(ws, pid)  # provider hook, keeper, secret, row
    await automation_schedule.delete(ws, sid)
    await automations_repo.delete_schedule(ws, sid)


@router.delete("/{sid}", status_code=204)
async def delete_automation(sid: str, auth: AuthContext = Depends(get_current_user)) -> Response:
    ws = await _ws(auth)
    _may_edit(await _row(ws, sid, auth), auth)
    await _destroy(ws, sid)
    return Response(status_code=204)


@router.post("/{sid}/run")
async def run_now(sid: str, auth: AuthContext = Depends(get_current_user)) -> dict:
    """Run now: today's chat gate first (§14.10), one run at a time, then a `TriggerFireWorkflow`."""
    ws = await _ws(auth)
    row = await _row(ws, sid, auth)
    _may_edit(row, auth)
    if row.get("match"):
        raise _err(409, "rule_never_runs")  # observe records; Run now would be the fire path
    if row["status"] != "active":
        raise _err(409, "not_active", status=row["status"])
    gate = await chat_gate.gate_chat(user_id=auth.user_id, owner_id=ws, session_key=f"automation:{sid}")
    if gate.get("blocked"):
        raise _err(402, gate.get("code") or "blocked", message=gate.get("message"))
    if row.get("activeRunTaskId"):
        raise _err(409, "run_in_progress", taskId=row["activeRunTaskId"])
    ulid = str(ULID())
    try:
        await temporal_client.get_client().start_workflow(
            "TriggerFireWorkflow",
            FireInput(ws, sid, "manual", dedupe_key=ulid, actor_user_id=auth.user_id),
            id=f"fire:{ws}:{sid}:manual:{ulid}",
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )
    except (temporal_client.TemporalUnavailable, RPCError) as exc:
        raise _err(503, "TEMPORAL_UNAVAILABLE", retryable=True) from exc
    return {"triggerId": f"manual:{sid}:{ulid}"}
