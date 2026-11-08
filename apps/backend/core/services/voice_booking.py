"""Where a phone booking goes: the Agent page's `bookingsGo`. "google" is the owner's own Google Calendar
(the workspace's existing grant, `access_token.get(ws, "google")`); anything
else is Nova's own calendar (routers/voice_phone `VOICEBOOK#` rows and `VOICECELL#` cells), unchanged.

Google Calendar API v3, on the primary calendar, as Google documents it:
    freeBusy.query  https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query
    events.insert   https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
    events.patch    https://developers.google.com/workspace/calendar/api/v3/reference/events/patch
    events.delete   https://developers.google.com/workspace/calendar/api/v3/reference/events/delete

With Google, Nova's hold (row + cells, one transaction) is written first, so a booking never double-books Nova's
own slots and survives Google failing. Each Google call gets GOOGLE_S; a failure, a timeout or an expired or missing
grant keeps the hold and raises one Line-problem per workspace.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services.credentials import access_token

logger = logging.getLogger(__name__)

GOOGLE_S = 2.0  # token + request, per Google call (the speed rule's integration bound)
API = "https://www.googleapis.com/calendar/v3"
EVENTS = f"{API}/calendars/primary/events"
_PROBLEM_SK = "VOICELINEPROBLEM#google"  # -> the open Line-problem's task, so a workspace has at most one
_OPEN = ("queued", "working", "waiting")  # tasks_schema.TaskStatus; idle (Done), resolved, failed are closed


async def warm_google(ws: str) -> None:
    """Fetch (and so cache) the workspace's Google token at a call's start, so the first in-call Google call is
    not a cold one. Fire-and-forget; never raises."""
    try:
        await access_token.get(ws, "google")
    except Exception:  # noqa: BLE001 -- not connected, or a failed fetch: the booking call will say so
        logger.debug("google warm-up skipped ws=%s", ws)


def _vp():
    from routers import voice_phone  # lazy: the router imports this module

    return voice_phone


def _google(cfg: dict) -> bool:
    return cfg.get("bookingsGo") == "google"


async def _request(ws: str, method: str, url: str, body: dict | None = None) -> dict:
    async def go() -> dict:
        token = await access_token.get(ws, "google")
        async with httpx.AsyncClient(timeout=GOOGLE_S) as c:
            r = await c.request(method, url, headers={"Authorization": f"Bearer {token}"}, json=body)
            r.raise_for_status()
            return r.json() if r.content else {}

    return await asyncio.wait_for(go(), GOOGLE_S)


async def _try(ws: str, cfg: dict, call_id: str | None, method: str, url: str, body: dict | None = None) -> dict | None:
    """The Google answer, or None after raising the Line-problem. Never raises: the hold is the booking."""
    try:
        return await _request(ws, method, url, body)
    except Exception:  # noqa: BLE001 -- expired grant, timeout, 4xx/5xx: all keep the hold
        logger.warning("google calendar %s failed ws=%s", method, ws, exc_info=True)
    if not call_id:  # a read outside a call (business_profile): no call to raise the Line problem on
        return None
    try:
        await _line_problem(ws, cfg, call_id)
    except Exception:  # noqa: BLE001 -- the problem card is best-effort; the call goes on
        logger.warning("line problem not raised ws=%s", ws, exc_info=True)
    return None


async def _line_problem(ws: str, cfg: dict, call_id: str) -> None:
    """One open Line-problem per workspace: "Google Calendar sign-in expired; N booking(s) held in Nova's calendar".
    ponytail: it rides the existing phone Questions path (start_work -> a Worker task that asks the owner, kind
    `question`); Task 6 adds the `line_problem` kind and renames it."""
    vp, pk = _vp(), messaging_repo.owner_pk(ws)
    pointer = await messaging_repo._get(pk, _PROBLEM_SK)
    if pointer and (await tasks_repo.get_task(ws, pointer["taskId"]) or {}).get("status") in _OPEN:  # fmt: skip
        return
    now = vp._now(cfg).strftime("%Y-%m-%dT%H:%M")
    held = sum(1 for b in await vp._bookings(ws, "") if not b.get("eventId") and b["sk"][10:] >= now)
    name = cfg.get("name", "").strip() or "Lightbulb"
    title = f"Google Calendar sign-in expired; {held} booking(s) held in {name}'s calendar"
    brief = (
        f"{title}. Phone bookings are safe in {name}'s own calendar but are not in Google Calendar. Ask the owner "
        "to reconnect Google in Settings. Do nothing else until they answer."
    )
    out = await vp.start_work(call_id, vp.WorkIn(title=title, brief=brief))
    await run_in_thread(messaging_repo._table().put_item, Item={"pk": pk, "sk": _PROBLEM_SK, "taskId": out["taskId"]})


def _event(cfg: dict, call: dict, start: str, service: str, name: str) -> dict:
    vp = _vp()
    end = datetime.strptime(start, "%Y-%m-%dT%H:%M") + timedelta(minutes=vp._minutes(cfg, service))
    tz = vp._tz(cfg).key
    return {
        "summary": f"{service} · {name}",
        "description": f"Booked by phone ({call.get('caller') or 'unknown number'}).",
        "start": {"dateTime": f"{start}:00", "timeZone": tz},
        "end": {"dateTime": f"{end:%Y-%m-%dT%H:%M}:00", "timeZone": tz},
    }


async def _keep(ws: str, start: str, event_id: str) -> str:
    if event_id:
        await run_in_thread(
            messaging_repo._table().update_item,
            Key={"pk": messaging_repo.owner_pk(ws), "sk": f"VOICEBOOK#{start}"},
            UpdateExpression="SET eventId = :e",
            ExpressionAttributeValues={":e": event_id},
        )
    return event_id


async def free(ws: str, cfg: dict, call_id: str | None, date: str, minutes: int) -> list[str]:
    """Open half-hour starts on `date`: opening hours minus Nova's own bookings, and with Google minus the primary
    calendar's busy blocks (a failed freeBusy offers Nova's slots only)."""
    vp = _vp()
    taken = await vp._taken(ws, date)
    window = vp._open_window(cfg, date) if _google(cfg) else None
    if window:
        tz = vp._tz(cfg)
        body = {"timeMin": window[0].replace(tzinfo=tz).isoformat(), "timeMax": window[1].replace(tzinfo=tz).isoformat(),
                "timeZone": tz.key, "items": [{"id": "primary"}]}  # fmt: skip
        taken += _blocks(await _try(ws, cfg, call_id, "POST", f"{API}/freeBusy", body), tz)
    return vp.free_slots(cfg, date, taken, vp._now(cfg), minutes)


def _blocks(answer: dict | None, tz) -> list[tuple[datetime, datetime]]:
    """A freeBusy answer's primary-calendar busy blocks, in the business's local time."""
    busy = (answer or {}).get("calendars", {}).get("primary", {}).get("busy", [])
    return [tuple(datetime.fromisoformat(b[k]).astimezone(tz).replace(tzinfo=None) for k in ("start", "end")) for b in busy]  # fmt: skip


async def busy_across(ws: str, cfg: dict, dates: list[str], bound: float) -> list[tuple[datetime, datetime]]:
    """The primary calendar's busy blocks over `dates`' opening hours: one freeBusy for all of them, `bound` s at
    most. [] when bookings do not go to Google, or Google is slow, refused or not signed in: the open slots are
    then the hours minus Nova's own bookings, as before Google (google_grant warns once that it needs reconnecting)."""
    vp = _vp()
    windows = [w for d in dates if (w := vp._open_window(cfg, d))] if _google(cfg) else []
    if not windows:
        return []
    tz = vp._tz(cfg)
    body = {"timeMin": windows[0][0].replace(tzinfo=tz).isoformat(), "timeMax": windows[-1][1].replace(tzinfo=tz).isoformat(),
            "timeZone": tz.key, "items": [{"id": "primary"}]}  # fmt: skip
    try:
        return _blocks(await asyncio.wait_for(_request(ws, "POST", f"{API}/freeBusy", body), bound), tz)
    except Exception as exc:  # noqa: BLE001 -- the profile still offers Nova's own slots
        logger.info("google freeBusy skipped for the profile ws=%s (%s)", ws, type(exc).__name__)
        return []


async def _busy(ws: str, cfg: dict, start: str, minutes: int) -> bool:
    """Whether the primary calendar is busy in [start, start + minutes). A failed check is "not busy": book as before."""
    tz = _vp()._tz(cfg)
    t = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    body = {
        "timeMin": t.isoformat(),
        "timeMax": (t + timedelta(minutes=minutes)).isoformat(),
        "items": [{"id": "primary"}],
    }
    try:
        return bool((await _request(ws, "POST", f"{API}/freeBusy", body))["calendars"]["primary"].get("busy"))
    except Exception:  # noqa: BLE001 -- the insert that follows raises the Line-problem if Google is down
        logger.warning("google freeBusy at book failed ws=%s", ws, exc_info=True)
        return False


async def book(ws: str, cfg: dict, call_id: str, call: dict, start: str, service: str, name: str) -> str:
    """Nova's hold first (Task 2's transaction), then the Google event; the event id ("" if none) is on the row."""
    vp = _vp()
    await vp._book(ws, call_id, call, cfg, start, service, name)
    if not _google(cfg):
        return ""
    if await _busy(ws, cfg, start, vp._minutes(cfg, service) + int(cfg.get("bufferMin") or 0)):
        pk = messaging_repo.owner_pk(ws)
        row = await messaging_repo._get(pk, f"VOICEBOOK#{start}")
        await messaging_repo._transact(vp._release(messaging_repo.settings.MESSAGING_TABLE, pk, row))
        raise vp._err(409, "slot_taken")  # busy in the owner's real calendar: Nova offers another time
    got = await _try(ws, cfg, call_id, "POST", EVENTS, _event(cfg, call, start, service, name))
    return await _keep(ws, start, (got or {}).get("id", ""))


async def move(ws: str, cfg: dict, call_id: str, call: dict, row: dict, new_start: str) -> str:
    """Move the hold, then patch its Google event (or insert one for a booking only Nova held)."""
    await _vp()._book(ws, call_id, call, cfg, new_start, row["service"], row["name"], old=row)
    if not _google(cfg):
        return ""
    ev, old_id = _event(cfg, call, new_start, row["service"], row["name"]), row.get("eventId", "")
    got = await (_try(ws, cfg, call_id, "PATCH", f"{EVENTS}/{old_id}", ev) if old_id else _try(ws, cfg, call_id, "POST", EVENTS, ev))  # fmt: skip
    return await _keep(ws, new_start, (got or {}).get("id") or old_id)


async def cancel(ws: str, cfg: dict, call_id: str, row: dict) -> None:
    vp = _vp()
    await messaging_repo._transact(
        vp._release(messaging_repo.settings.MESSAGING_TABLE, messaging_repo.owner_pk(ws), row)
    )
    if _google(cfg) and row.get("eventId"):
        await _try(ws, cfg, call_id, "DELETE", f"{EVENTS}/{row['eventId']}")
