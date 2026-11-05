"""Proactive rules, observe mode (contracts §18, spec 12 §3.4): record what a rule would have done.

`record_match` is an Activity and is also awaited directly by admission. It never creates a task, a Router turn
or a push: in wave 2 every mode only records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from temporalio import activity

from core import tasks_schema as ts
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric
from core.repositories import automations_repo, tasks_repo
from core.repositories.tasks_repo import VersionConflict, _put_new, _row
from core.services import automation_hooks, google_watch, rules
from core.services.rules import DID_IT_YOURSELF_WINDOW_H
from core.services.super_agent_flags import flags_for

logger = logging.getLogger(__name__)


@activity.defn
async def record_match(
    ws: str,
    sched_id: str,
    version: int,
    thread_key: str,
    ref: dict[str, Any],
    p: float | None,
    would_do: str,
    source: Literal["chat", "webhook"],
) -> Literal["observed", "duplicate"]:
    """One match per rule per thread, ever: the deterministic `TRIG#rule:…` put is the dedupe. The stats entry and
    the `OBS#` marker are idempotent too, so a retry after a crash between writes completes the record."""
    trigger_id = ts.rule_trigger_id(sched_id, thread_key)
    now = utc_now_iso()
    _, new = await tasks_repo.put_trigger(
        ws,
        {
            "triggerId": trigger_id,
            "source": source,
            "ref": ref,
            "admission": "candidate",
            "lane": "shared",
            "receivedAt": now,
            "ruleMatch": {"schedId": sched_id, "version": version, "p": p, "decision": "observed", "wouldDo": would_do},
        },
    )
    entry = {"at": now, "threadKey": thread_key, "wouldDo": would_do, "version": version, "doneByHuman": False}
    await automations_repo.append_match(ws, sched_id, entry)
    await _put_new(
        ws, _row(ts.Observed, ws, {"threadKey": thread_key, "schedId": sched_id, "triggerId": trigger_id, "at": now})
    )
    outcome = "observed" if new else "duplicate"
    put_metric("rules.match", dimensions={"outcome": outcome})
    return outcome


async def note_task_started(ws: str, thread_key: str) -> None:
    """Called when a person's request starts or passes through to a task on `thread_key`: every rule that
    observed the thread in the last 24 h gets `doneByHuman=true`. Zero model cost; never raises (it must not
    fail the Router apply)."""
    try:
        obs = await tasks_repo._get(ws, ts.observed_sk(thread_key))
        if obs is None:
            return
        at = datetime.fromisoformat(obs["at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - at > timedelta(hours=DID_IT_YOURSELF_WINDOW_H):
            return
        for rule in await automations_repo.list_rules(ws):
            entries = (rule.get("stats") or {}).get("matches") or []
            if any(m.get("threadKey") == thread_key for m in entries):
                await automations_repo.mark_done_by_human(ws, rule["scheduleId"], thread_key)
    except Exception:  # noqa: BLE001 -- measurement only
        logger.warning("did-it-yourself mark failed for %s", thread_key, exc_info=True)


#: Google's answer to a stale sync token (HTTP 410, reason `fullSyncRequired`); gog surfaces the last stderr line.
_TOKEN_EXPIRED = re.compile(r"\b410\b|fullSyncRequired|sync token", re.IGNORECASE)


async def _list_events(ws: str, account: str, token: str | None) -> tuple[list[dict], str | None]:
    """Google Calendar `events.list` on the account's primary calendar through gog, every page:
    `(items, nextSyncToken)`. No token is the first sync, from now on. A token Google has expired (410) starts
    over the same way, skipping the gap; any other failure is raised with the token intact so the push is
    retried (the route answers 503)."""

    def run(sync_token: str | None) -> tuple[list[dict], str | None]:
        env = google_watch._gog_env(ws)
        params: dict[str, str] = {"calendarId": "primary"}
        params |= {"syncToken": sync_token} if sync_token else {"timeMin": datetime.now(timezone.utc).isoformat()}
        items: list[dict] = []
        page = None
        while True:
            paged = params | ({"pageToken": page} if page else {})
            argv = ["gog", "api", "call", "calendar", "v3", "events.list", "--params", json.dumps(paged), "-a", account]
            data = json.loads(google_watch._run_gog(argv, env))
            items += data.get("items") or []
            page = data.get("nextPageToken")
            if not page:
                return items, data.get("nextSyncToken")

    try:
        return await asyncio.to_thread(run, token)
    except ValueError as exc:
        if not token or not _TOKEN_EXPIRED.search(str(exc)):
            raise
        logger.info("calendar sync token expired for %s: starting over", account)
        _, fresh = await asyncio.to_thread(run, None)
        return [], fresh


async def _sync(ws: str, hook: dict) -> tuple[list[dict], str | None]:
    """The open invites since the hook's last sync, and the sync token to store once they are recorded (None =
    unchanged). The first sync matches nothing: invites that predate the rule are not its business."""
    token = hook.get("pageToken")
    items, new_token = await _list_events(ws, hook["account"], token)
    now = datetime.now(timezone.utc)
    invites = [e for e in items if rules.open_invite(e, hook["account"], now)] if token else []
    return invites, (new_token if new_token and new_token != token else None)


async def _store_token(ws: str, public_id: str, new_token: str | None) -> None:
    if new_token:
        await automation_hooks._update_hook(ws, public_id, {"pageToken": new_token})


@activity.defn
async def calendar_invites(ws: str, public_id: str) -> list[dict]:
    """The open invites on the hook's account since its last sync (spec 12 §3.3; code only, no model). The sync
    token lives on `HOOK#.pageToken` (A8's keeper never overwrites it)."""
    hook = await automations_repo.get_hook(ws, public_id)
    if hook is None or not hook.get("account"):
        return []
    invites, new_token = await _sync(ws, hook)
    await _store_token(ws, public_id, new_token)
    return invites


async def calendar_rule_fire(ws: str, hook: dict) -> bool:
    """The hook-fire branch for a calendar rule (`routers/hooks.py`): True when the hook belongs to a rule row,
    which then never starts `TriggerFireWorkflow`. Each new open invite becomes one observe record, and the sync
    token moves only after the records are written: the `TRIG#` put is the dedupe, so a crash in between re-records
    nothing and loses nothing. A rule that is off or paused still advances its token so turning it on never
    matches a backlog. A rescheduled invite keeps its event id, so it is the same thread and the same one match.
    ponytail: runs inline in the push handler (one gog call, seconds); a workflow if Google retries pile up."""
    rule = await automations_repo.get_schedule(ws, hook["scheduleId"])
    if not rule or (rule.get("match") or {}).get("sources") != ["calendar"]:
        return False
    if not await flags_for(ws):  # the switch off: a rule's hook does nothing, and never a workflow either
        return True
    invites, new_token = await _sync(ws, hook)
    if rule.get("status") == "active" and rule.get("mode") in ("observe", "auto"):
        account = hook["account"]
        for event in invites:
            start = rules.event_start(event)
            ref = {
                "eventId": event["id"],
                "title": event.get("summary") or "",
                "start": start.isoformat() if start else "",
            }
            try:
                await record_match(
                    ws,
                    rule["scheduleId"],
                    rule.get("ruleVersion", 1),
                    f"cal:{account}:{event['id']}",
                    ref,
                    None,
                    rules.would_do(rule, ref, rule.get("tz") or "UTC"),
                    "webhook",
                )
            except VersionConflict:
                logger.warning("invite prep: record_match conflict for %s", rule["scheduleId"], exc_info=True)
    await _store_token(ws, hook["publicId"], new_token)
    return True


WORKFLOWS: list = []
ACTIVITIES = [record_match, calendar_invites]
