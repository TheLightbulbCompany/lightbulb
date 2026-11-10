"""Canonical phone booking effects, beside the existing row/cell reservation owner.

New Google holds persist concrete calendar/event identity and one frozen pending
operation before a write. Only verified provider results can settle a local
change; unknown results preserve reservations and permit read-only reconciliation.
Existing records without a verifiable binding refuse provider mutation rather
than guessing from today's Agent configuration. Own-calendar bookings stay local.

Google Calendar API v3: freeBusy.query, events.list/get/insert/patch/delete.
https://developers.google.com/workspace/calendar/api/v3/reference/events/list
https://developers.google.com/workspace/calendar/api/guides/version-resources
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import quote, urlencode

from botocore.exceptions import ClientError
from datetime import datetime, timedelta

import httpx

from core.dynamodb import run_in_thread
from core.repositories import messaging_repo, tasks_repo
from core.services.credentials import access_token

logger = logging.getLogger(__name__)

GOOGLE_S = 2.0  # token + request, per Google call (the speed rule's integration bound)
# A write sent this long ago (5 x its own bound) has finished at Google, so "not applied" evidence is final.
# Before it the request may still be in flight and an absent or unchanged event proves nothing.
SETTLE_S = 5 * GOOGLE_S
API = "https://www.googleapis.com/calendar/v3"
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


async def _request(ws: str, method: str, url: str, body: dict | None = None, *, etag: str | None = None) -> dict:
    async def go() -> dict:
        token = await access_token.get(ws, "google")
        async with httpx.AsyncClient(timeout=GOOGLE_S) as c:
            r = await c.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", **({"If-Match": etag} if etag else {})},
                json=body,
            )
            r.raise_for_status()
            answer = r.json() if r.content else {}
            if not isinstance(answer, dict):
                raise ValueError("invalid provider response")
            return answer

    return await asyncio.wait_for(go(), GOOGLE_S)


async def _try(ws: str, cfg: dict, call_id: str | None, method: str, url: str, body: dict | None = None) -> dict | None:
    """Read answer or None after surfacing a problem; callers must not interpret None as free."""
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
    """One existing question per workspace for external booking failures/uncertainty.
    ponytail: it rides the existing phone Questions path (start_work -> a Worker task that asks the owner, kind
    `question`); Task 6 adds the `line_problem` kind and renames it."""
    vp, pk, table, now_s = _vp(), messaging_repo.owner_pk(ws), messaging_repo._table(), int(time.time())
    pointer = await messaging_repo._get(pk, _PROBLEM_SK)
    if pointer and pointer.get("taskId"):
        if (await tasks_repo.get_task(ws, pointer["taskId"]) or {}).get("status") in _OPEN:
            return
    elif pointer and int(pointer.get("claimUntil", 0)) > now_s:
        return  # another failure is raising it now
    # Claim the pointer against what was read, so concurrent failures raise one card; a claim whose card never
    # got raised expires after a minute.
    claim = {"pk": pk, "sk": _PROBLEM_SK, "claim": uuid.uuid4().hex, "claimUntil": now_s + 60}
    condition = (
        {"ConditionExpression": "taskId = :seen OR claim = :seen",
         "ExpressionAttributeValues": {":seen": pointer.get("taskId") or pointer.get("claim")}}
        if pointer else {"ConditionExpression": "attribute_not_exists(sk)"}
    )  # fmt: skip
    try:
        await run_in_thread(table.put_item, Item=claim, **condition)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    now = vp._now(cfg).strftime("%Y-%m-%dT%H:%M")
    held = sum(1 for b in await vp._bookings(ws, "") if (not confirmed(b) or b.get("pending")) and b["sk"][10:] >= now)
    title = f"Google Calendar needs checking; {held} booking(s) unconfirmed"
    brief = (
        f"{title}. External booking outcomes may be unknown; preserve holds and verify the provider before changing them. Ask the owner "
        "to check Google in Settings and reconcile the recorded booking operation. Do not assume sign-in expired."
    )
    out = await vp.start_work(call_id, vp.WorkIn(title=title, brief=brief))
    await run_in_thread(table.put_item, Item={"pk": pk, "sk": _PROBLEM_SK, "taskId": out["taskId"]})


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


async def free(ws: str, cfg: dict, call_id: str | None, date: str, minutes: int,
               lanes: list[tuple[str, str]] = (("", ""),)) -> list[str]:  # fmt: skip
    """Open half-hour starts on `date` where one of `lanes` (the people who can do it) is free: opening hours minus
    Nova's own bookings in that lane, and with Google minus the primary calendar's busy blocks, which every lane
    shares (a failed freeBusy is unavailable)."""
    vp = _vp()
    taken = await vp._taken(ws, date)
    window = vp._open_window(cfg, date) if _google(cfg) else None
    if window:
        tz = vp._tz(cfg)
        body = {"timeMin": window[0].replace(tzinfo=tz).isoformat(), "timeMax": window[1].replace(tzinfo=tz).isoformat(),
                "timeZone": tz.key, "items": [{"id": "primary"}]}  # fmt: skip
        taken += _blocks(await _try(ws, cfg, call_id, "POST", f"{API}/freeBusy", body), tz)
    return vp.open_starts(cfg, date, taken, vp._now(cfg), minutes, list(lanes))


def _blocks(answer: dict | None, tz, calendar_id: str = "primary") -> list[tuple[datetime, datetime]]:
    """A freeBusy answer's primary-calendar busy blocks, in the business's local time."""
    try:
        calendar = answer["calendars"][calendar_id]
        if not isinstance(calendar, dict) or calendar.get("errors") or not isinstance(calendar.get("busy"), list):
            raise ValueError("invalid calendar availability")
        blocks = []
        for block in calendar["busy"]:
            start, end = (datetime.fromisoformat(block[k]) for k in ("start", "end"))
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError("invalid busy interval")
            blocks.append((start.astimezone(tz).replace(tzinfo=None), end.astimezone(tz).replace(tzinfo=None)))
        return blocks
    except (KeyError, ValueError, TypeError):
        raise _vp()._err(503, "provider_availability_unknown") from None


async def busy_across(ws: str, cfg: dict, dates: list[str], bound: float) -> list[tuple[datetime, datetime]]:
    """The primary calendar's busy blocks over `dates`' opening hours: one freeBusy for all of them, `bound` s at
    most. [] only when bookings do not go to Google or there are no open windows.
    A failed provider read raises: local slots are not externally verified."""
    vp = _vp()
    windows = [w for d in dates if (w := vp._open_window(cfg, d))] if _google(cfg) else []
    if not windows:
        return []
    tz = vp._tz(cfg)
    body = {"timeMin": windows[0][0].replace(tzinfo=tz).isoformat(), "timeMax": windows[-1][1].replace(tzinfo=tz).isoformat(),
            "timeZone": tz.key, "items": [{"id": "primary"}]}  # fmt: skip
    try:
        return _blocks(await asyncio.wait_for(_request(ws, "POST", f"{API}/freeBusy", body), bound), tz)
    except Exception as exc:  # noqa: BLE001 -- facts survive; provider availability does not
        logger.info("google freeBusy skipped for the profile ws=%s (%s)", ws, type(exc).__name__)
        raise _vp()._err(503, "provider_availability_unknown") from None


def row_condition(row: dict) -> tuple[str, dict]:
    """Exact observed row, including legacy identity, never a start-time-only delete."""
    if row.get("bookingId"):
        return "attribute_exists(sk) AND bookingId = :bid AND generation = :g", {
            ":bid": row["bookingId"],
            ":g": row["generation"],
        }
    # Old records cannot be provider-rebound; local releases still compare their
    # original creation/call fingerprint in the same transaction as cell deletes.
    return "attribute_exists(sk) AND createdAt = :created AND callId = :call", {
        ":created": row.get("createdAt", ""),
        ":call": row.get("callId", ""),
    }


def confirmed(row: dict) -> bool:
    """The booking stands. A row written before `bookingState` existed was always a confirmed own-calendar booking."""
    return (row.get("bookingState") or "confirmed") == "confirmed"


def _result(row: dict | None, state: str, code: str = "", *, action: str = "create") -> dict:
    return {
        "state": state,
        "code": code,
        "action": action,
        "provider": (row or {}).get("bookingProvider", "google"),
        "operationId": ((row or {}).get("pending") or {}).get("id"),
        "currentStart": _vp()._start(row) if row and confirmed(row) else None,
        "requestedStart": ((row or {}).get("pending") or {}).get("newStart"),
        "staff": (row or {}).get("staff", ""),
    }


async def _read(ws: str, sk: str) -> dict | None:
    return (
        await run_in_thread(
            messaging_repo._table().get_item, Key={"pk": messaging_repo.owner_pk(ws), "sk": sk}, ConsistentRead=True
        )
    ).get("Item")


async def _transaction(items: list[dict]) -> None:
    if len(items) > 100:
        raise _vp()._err(400, "booking_too_long")
    try:
        await messaging_repo._transact(items)
    except ClientError as exc:
        if messaging_repo.is_cancelled(exc):
            raise _vp()._err(409, "booking_changed") from None
        raise


async def _save(row: dict, new: dict, extra: list[dict] | None = None) -> dict:
    condition, values = row_condition(row)
    new = {**new, "generation": int(row["generation"]) + 1}
    await _transaction(
        [
            {
                "Put": {
                    "TableName": messaging_repo.settings.MESSAGING_TABLE,
                    "Item": new,
                    "ConditionExpression": condition,
                    "ExpressionAttributeValues": values,
                }
            },
            *(extra or []),
        ]
    )
    return new


def _events(row: dict) -> str:
    return f"{API}/calendars/{quote(row['calendarId'], safe='')}/events"


async def _busy(ws: str, cfg: dict, start: str, minutes: int, calendar_id: str = "primary") -> bool:
    tz = _vp()._tz(cfg)
    t = datetime.strptime(start, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
    body = {
        "timeMin": t.isoformat(),
        "timeMax": (t + timedelta(minutes=minutes)).isoformat(),
        "items": [{"id": calendar_id}],
    }
    try:
        return bool(_blocks(await _request(ws, "POST", f"{API}/freeBusy", body), tz, calendar_id))
    except Exception:
        raise _vp()._err(503, "provider_availability_unknown") from None


async def _problem(ws: str, cfg: dict, call_id: str) -> None:
    try:
        await _line_problem(ws, cfg, call_id)
    except Exception:
        logger.warning("booking problem could not be recorded ws=%s", ws)


def _matches(row: dict, event: dict, body: dict) -> bool:
    try:
        marker = (event.get("extendedProperties") or {}).get("private") or {}
        return (
            event.get("id") == row["eventId"]
            and event.get("status") == "confirmed"
            and isinstance(event.get("etag"), str)
            and bool(event["etag"])
            and marker.get("lightbulbBookingId") == row["bookingId"]
            and marker.get("lightbulbOperationId") == row["pending"]["id"]
            and all(_instant(event.get(k)) == _instant(body.get(k)) for k in ("start", "end"))
        )
    except (AttributeError, TypeError, KeyError):
        return False


def _instant(value: dict | None) -> datetime | None:
    try:
        from zoneinfo import ZoneInfo

        stamp = datetime.fromisoformat(value["dateTime"])
        return stamp.replace(tzinfo=ZoneInfo(value["timeZone"])) if stamp.tzinfo is None else stamp
    except (TypeError, ValueError, KeyError):
        return None


# A reservation cell the table's TTL already removed; one that exists must still be this booking's.
_OURS = "attribute_not_exists(sk) OR (bookingId = :bid AND booking = :old)"


def _drop_target(row: dict) -> dict:
    return {
        "Delete": {
            "TableName": messaging_repo.settings.MESSAGING_TABLE,
            "Key": {"pk": row["pk"], "sk": row["pending"]["targetHold"]},
            "ConditionExpression": "bookingId = :bid AND operationId = :op",
            "ExpressionAttributeValues": {":bid": row["bookingId"], ":op": row["pending"]["id"]},
        }
    }


async def _settle(row: dict, event: dict | None) -> dict:
    """Only exact provider evidence reaches here; all row/cell changes are one CAS transaction."""
    vp, pending = _vp(), row["pending"]
    action, table, pk = pending["action"], messaging_repo.settings.MESSAGING_TABLE, row["pk"]
    if action == "cancel":
        await _transaction(vp._release(table, pk, row))
        return _result(row, "confirmed", action=action)
    new = {**row, "bookingState": "confirmed", "providerEvent": event, "eventId": event["id"]}
    del new["pending"]
    if action == "create":
        await _save(row, new)
    else:
        new_sk, new_cells = f"VOICEBOOK#{pending['newStart']}{row['sk'][26:]}", pending["targetCells"]  # same lane
        new |= {"sk": new_sk, "cells": new_cells, "generation": int(row["generation"]) + 1}
        # Delete old row/old-only cells; common cells get exactly one update.
        items = vp._release(table, pk, row, keep=set(new_cells))
        items.append(_drop_target(row))
        items.append({"Put": {"TableName": table, "Item": new, "ConditionExpression": "attribute_not_exists(sk)"}})
        for cell in new_cells:
            items.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            "pk": pk,
                            "sk": cell,
                            "booking": new_sk,
                            "bookingId": row["bookingId"],
                            "expiresAt": vp._cell_expiry(cell),
                        },
                        "ConditionExpression": _OURS,
                        "ExpressionAttributeValues": {":bid": row["bookingId"], ":old": row["sk"]},
                    }
                }
            )
        await _transaction(items)
    return _result(row, "confirmed", action=action)


async def _fail(row: dict, cfg: dict, call_id: str, *, rejected: bool) -> dict:
    vp, pending = _vp(), row["pending"]
    table, pk, action = messaging_repo.settings.MESSAGING_TABLE, row["pk"], pending["action"]
    if rejected:
        if action == "create":
            await _transaction(vp._release(table, pk, row))
        else:
            new = {k: v for k, v in row.items() if k != "pending"}
            extra = [
                {
                    "Delete": {
                        "TableName": table,
                        "Key": {"pk": pk, "sk": cell},
                        "ConditionExpression": _OURS,
                        "ExpressionAttributeValues": {":bid": row["bookingId"], ":old": row["sk"]},
                    }
                }
                for cell in pending.get("targetCells", [])
                if cell not in row["cells"]
            ]
            if action == "move":
                extra.append(_drop_target(row))
            await _save(row, new, extra)
        return _result(row, "rejected", "provider_rejected", action=action)
    new = {**row, "pending": {**pending, "state": "unknown"}}
    await _save(row, new)
    await _problem(row["pk"].removeprefix("OWNER#"), cfg, call_id)
    return _result(new, "pending", "provider_outcome_unknown", action=action)


async def _dispatch(ws: str, cfg: dict, call_id: str, row: dict) -> dict:
    pending = row["pending"]
    row = await _save(row, {**row, "pending": {**pending, "state": "sending", "sentAt": int(time.time())}})
    pending = row["pending"]
    try:
        event = await _request(ws, pending["method"], pending["url"], pending.get("body"), etag=pending.get("etag"))
    except (Exception, asyncio.CancelledError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        if status == 409 and pending["action"] == "create":
            return await _resume(ws, cfg, call_id, row, "create")  # the deterministic id exists: an earlier send landed
        # A documented precondition/authorization rejection proves THIS
        # dispatch did not apply. Lost/5xx/interrupted responses are ambiguous.
        rejected = status in (
            400,
            401,
            403,
            404,
            412,
            422,
            429,
        )
        return await asyncio.shield(_fail(row, cfg, call_id, rejected=rejected))
    if pending["action"] == "cancel":
        return await _settle(row, None)  # successful bound DELETE (204)
    if _matches(row, event, pending["body"]):
        return await _settle(row, event)
    return await _fail(row, cfg, call_id, rejected=False)


async def _calendar_readable(ws: str, row: dict) -> bool:
    # An inaccessible calendar also answers 404 for its events; only a readable bound calendar makes one absent.
    try:
        calendar = await _request(ws, "GET", f"{API}/calendars/{quote(row['calendarId'], safe='')}")
    except Exception:  # noqa: BLE001 -- unreadable is not evidence
        return False
    return calendar.get("id") == row["calendarId"]


async def _resume(ws: str, cfg: dict, call_id: str, row: dict, action: str, new_start: str | None = None) -> dict:
    """Reconcile from what Google shows; a write is repeated only with its frozen identity, once it cannot still
    be in flight."""
    pending = row["pending"]
    if (pending["action"], pending.get("newStart")) != (action, new_start):
        return _result(row, "pending", "operation_pending", action=action)
    if pending["state"] == "prepared":
        return await _dispatch(ws, cfg, call_id, row)  # no HTTP was sent before the `sending` save
    # A row sent before `sentAt` existed was sent long ago.
    settled = int(time.time()) - int(pending.get("sentAt", 0)) >= SETTLE_S
    event, absent = None, False
    try:
        event = await _request(ws, "GET", f"{_events(row)}/{quote(row['eventId'], safe='')}")
    except httpx.HTTPStatusError as exc:
        absent = exc.response.status_code in (404, 410) and await _calendar_readable(ws, row)
    except Exception:  # noqa: BLE001 -- unreadable: still unknown
        pass
    if action == "cancel" and (absent or (event or {}).get("status") == "cancelled"):
        return await _settle(row, None)
    if event and action != "cancel" and _matches(row, event, pending["body"]):
        return await _settle(row, event)
    if settled and event and action != "create" and event.get("status") == "confirmed" and event.get("etag") == pending["etag"]:  # fmt: skip
        # Google still serves the version the conditional write was sent against: it did not apply.
        return await _fail(row, cfg, call_id, rejected=True)
    if settled and absent and action == "create":
        return await _dispatch(ws, cfg, call_id, row)  # same event id: a duplicate answers 409
    return _result(row, "pending", "provider_outcome_unknown", action=action)


async def book(ws: str, cfg: dict, call_id: str, call: dict, start: str, service: str, name: str) -> dict:
    vp = _vp()
    caller = call.get("caller")
    # This call's (or number's) earlier Google hold at that start, with whichever person it took.
    existing = next(
        (
            b
            for b in await vp._bookings(ws, start)
            if b.get("bookingProvider") == "google"
            and (b.get("callId") == call_id or (caller and b.get("phone") == caller))
        ),
        None,
    )
    if existing is None:
        pass  # none: a new booking below
    elif (existing.get("service"), existing.get("name")) != (service, name):
        raise vp._err(409, "booking_changed")
    elif existing.get("pending"):
        return await _resume(ws, cfg, call_id, existing, "create")
    elif confirmed(existing):
        return _result(existing, "confirmed")
    elif existing.get("eventId"):
        return _result(existing, "held", "operation_requires_review")
    # else: a local hold never prepared (no POST precedes the prepare save): prepare it now.
    if not _google(cfg):
        row = await vp._book(ws, call_id, call, cfg, start, service, name)
        return {"state": "confirmed", "provider": "own", "staff": row.get("staff", "")}
    try:
        calendar = await _request(ws, "GET", f"{API}/calendars/primary")
        calendar_id = calendar["id"]
        if not isinstance(calendar_id, str) or not calendar_id or calendar_id == "primary":
            raise ValueError("concrete calendar identity required")
        if await _busy(ws, cfg, start, vp._minutes(cfg, service) + int(cfg.get("bufferMin") or 0), calendar_id):
            raise vp._err(409, "slot_taken")
    except Exception as exc:
        if getattr(exc, "status_code", None) == 409:
            raise
        await _problem(ws, cfg, call_id)
        return _result(None, "unavailable", "provider_availability_unknown")
    row = existing or await _read(ws, (await vp._book(ws, call_id, call, cfg, start, service, name))["sk"])
    operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-google:{row['bookingId']}:create").hex
    body = _event(cfg, call, start, service, name)
    body |= {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-google-event:{ws}:{row['bookingId']}").hex,
        "extendedProperties": {
            "private": {"lightbulbBookingId": row["bookingId"], "lightbulbOperationId": operation_id}
        },
    }
    row = await _save(
        row,
        {
            **row,
            "calendarId": calendar_id,
            "eventId": body["id"],
            "timezone": vp._tz(cfg).key,
            "pending": {
                "id": operation_id,
                "action": "create",
                "state": "prepared",
                "body": body,
                "method": "POST",
                "url": f"{API}/calendars/{quote(calendar_id, safe='')}/events",
            },
        },
    )
    return await _dispatch(ws, cfg, call_id, row)


async def _observe(ws: str, row: dict) -> dict:
    if not all(row.get(k) for k in ("bookingId", "calendarId", "eventId", "providerEvent")):
        raise _vp()._err(409, "booking_binding_unverified")
    event = await _request(ws, "GET", f"{_events(row)}/{quote(row['eventId'], safe='')}")
    observed = row["providerEvent"]
    if (
        event.get("id") != row["eventId"]
        or event.get("status") != "confirmed"
        or any(_instant(event.get(k)) != _instant(observed.get(k)) for k in ("start", "end"))
    ):
        raise _vp()._err(409, "provider_booking_changed")
    return event


async def _target_free(ws: str, row: dict, body: dict, buffer_min: int) -> bool:
    async with asyncio.timeout(GOOGLE_S):
        return await _target_events_free(ws, row, body, buffer_min)


async def _target_events_free(ws: str, row: dict, body: dict, buffer_min: int) -> bool:
    # Google Events.list supports expanded recurrence and pagination, but no
    # exclude-id filter. Exclude exactly this event in the complete result.
    start, end = _instant(body["start"]), _instant(body["end"])
    params = {
        "timeMin": (start - timedelta(minutes=buffer_min)).isoformat(),
        "timeMax": (end + timedelta(minutes=buffer_min)).isoformat(),
        "singleEvents": "true",
    }
    for _ in range(20):
        answer = await _request(ws, "GET", f"{_events(row)}?{urlencode(params)}")
        if not isinstance(answer.get("items"), list):
            raise _vp()._err(503, "provider_availability_unknown")
        for event in answer["items"]:
            if (
                event.get("id") == row["eventId"]
                or event.get("status") == "cancelled"
                or event.get("transparency") == "transparent"
            ):
                continue
            # Any other returned occupied event overlaps the expanded interval;
            # all-day and recurrence instances must never be subtracted with ours.
            return False
        if not answer.get("nextPageToken"):
            return True
        params["pageToken"] = answer["nextPageToken"]
    raise _vp()._err(503, "provider_availability_unknown")


def _own(row: dict) -> bool:
    """Our own calendar's booking. A row written before `bookingProvider` existed, with no Google event, is one too."""
    return row.get("bookingProvider") == "own" or not (row.get("bookingProvider") or row.get("eventId"))


async def _created(ws: str, cfg: dict, call_id: str, row: dict) -> dict:
    """The booking row once its pending create settled, else that create's result (it carries `state`)."""
    if (row.get("pending") or {}).get("action") != "create":
        return row
    result = await _resume(ws, cfg, call_id, row, "create")
    return await _read(ws, row["sk"]) if result["state"] == "confirmed" else result


async def move(ws: str, cfg: dict, call_id: str, call: dict, row: dict, new_start: str) -> dict:
    vp = _vp()
    if (row := await _created(ws, cfg, call_id, row)).get("state"):
        return row
    if row.get("pending"):
        return await _resume(ws, cfg, call_id, row, "move", new_start)
    if _own(row):
        await vp._book(ws, call_id, call, {**cfg, "bookingsGo": "own"}, new_start, row["service"], row["name"], old=row)
        return {"state": "confirmed", "provider": "own"}
    try:
        event = await _observe(ws, row)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 409:
            raise
        return _result(row, "unavailable", "provider_read_unavailable", action="move")
    if new_start == vp._start(row):
        return _result(row, "confirmed", action="move")
    cfg = {**cfg, "timezone": row["timezone"]}
    minutes = int(row["minutes"])
    if new_start not in vp.free_slots(cfg, new_start[:10], [], vp._now(cfg), minutes):
        raise vp._err(400, "slot_unavailable")
    start = datetime.strptime(new_start, "%Y-%m-%dT%H:%M").replace(tzinfo=vp._tz(cfg))
    end = start + (_instant(event["end"]) - _instant(event["start"]))
    operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{row['bookingId']}:{row['generation']}:move:{new_start}").hex
    body = {
        "start": {"dateTime": start.isoformat(), "timeZone": row["timezone"]},
        "end": {"dateTime": end.isoformat(), "timeZone": row["timezone"]},
        "extendedProperties": {
            "private": {
                **((event.get("extendedProperties") or {}).get("private") or {}),
                "lightbulbBookingId": row["bookingId"],
                "lightbulbOperationId": operation_id,
            }
        },
    }
    try:
        target_free = await _target_free(ws, row, body, int(cfg.get("bufferMin") or 0))
    except Exception:
        return _result(row, "unavailable", "provider_availability_unknown", action="move")
    if not target_free:
        raise vp._err(409, "slot_taken")
    lane = row["sk"][26:]  # a move keeps its person
    cells, table = vp._cells(cfg, new_start, minutes, lane), messaging_repo.settings.MESSAGING_TABLE
    # Refuse upfront if either prepare or final transfer would exceed DDB's cap.
    if len(set(row["cells"]) | set(cells)) + 3 > 100:
        raise vp._err(400, "booking_too_long")
    extra = [
        {
            "Put": {
                "TableName": table,
                "ConditionExpression": "attribute_not_exists(sk)",
                "Item": {
                    "pk": row["pk"],
                    "sk": cell,
                    "bookingId": row["bookingId"],
                    "booking": row["sk"],
                    "expiresAt": vp._cell_expiry(cell),
                },
            }
        }
        for cell in cells
        if cell not in row["cells"]
    ]
    target_key = f"VOICEHOLD#{new_start}#{row['bookingId']}"
    extra.append(
        {
            "Put": {
                "TableName": table,
                "ConditionExpression": "attribute_not_exists(sk)",
                "Item": {
                    "pk": row["pk"],
                    "sk": target_key,
                    "start": new_start,
                    "minutes": minutes,
                    "lane": lane,
                    "bookingId": row["bookingId"],
                    "operationId": operation_id,
                },
            }
        }
    )
    # Snapshot transaction prevents a new start row being occupied without cells.
    extra.append(
        {
            "ConditionCheck": {
                "TableName": table,
                "Key": {"pk": row["pk"], "sk": f"VOICEBOOK#{new_start}{lane}"},
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        }
    )
    row = await _save(
        row,
        {
            **row,
            "pending": {
                "id": operation_id,
                "action": "move",
                "state": "prepared",
                "method": "PATCH",
                "url": f"{_events(row)}/{quote(row['eventId'], safe='')}",
                "etag": event["etag"],
                "body": body,
                "newStart": new_start,
                "targetCells": cells,
                "targetHold": target_key,
            },
        },
        extra,
    )
    return await _dispatch(ws, cfg, call_id, row)


async def cancel(ws: str, cfg: dict, call_id: str, row: dict) -> dict:
    if (row := await _created(ws, cfg, call_id, row)).get("state"):
        return row
    if row.get("pending"):
        return await _resume(ws, cfg, call_id, row, "cancel")
    if _own(row) or not row.get("eventId"):  # a Google hold never prepared has no event: no POST precedes that save
        await _transaction(_vp()._release(messaging_repo.settings.MESSAGING_TABLE, messaging_repo.owner_pk(ws), row))
        return {"state": "confirmed", "provider": "own"}
    try:
        event = await _observe(ws, row)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 409:
            raise
        return _result(row, "unavailable", "provider_read_unavailable", action="cancel")
    operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{row['bookingId']}:{row['generation']}:cancel").hex
    row = await _save(
        row,
        {
            **row,
            "pending": {
                "id": operation_id,
                "action": "cancel",
                "state": "prepared",
                "method": "DELETE",
                "url": f"{_events(row)}/{quote(row['eventId'], safe='')}",
                "etag": event["etag"],
            },
        },
    )
    return await _dispatch(ws, cfg, call_id, row)
