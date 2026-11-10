"""Bounded Acuity Scheduling booking adapter; not yet connected to voice_booking (same state as Square's).

The canonical booking owner authorizes the effect and persists an immutable pending operation BEFORE calling.
No retries or local effects live here. AcuityError(outcome="unknown") must never become a confirmed booking,
a successful cancellation, or a new attempt: call find()/get() to reconcile first. Acuity has no idempotency
key, so a lost create is recognised by finding the appointment (type + start + caller), never by repeating it.

Auth: API key over HTTP Basic (user id : key), pasted by the owner. OAuth2 needs an Acuity-registered client.
Booked as the client (no admin=true), so Acuity itself validates availability and rejects double bookings.

Source contracts (checked 2026-11-10):
https://developers.acuityscheduling.com/reference/post-appointments
https://developers.acuityscheduling.com/reference/put-appointments-id-reschedule
https://developers.acuityscheduling.com/reference/get-availability-times
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx

from core.services import owner_lifecycle
from core.services.credentials import integration_secret_store

API = "https://acuityscheduling.com/api/v1"
HTTP_S = 2.0
OPERATION_S = 12.0


class AcuityError(RuntimeError):
    """Never carries provider bodies or credentials.

    rejected: no write was accepted. unavailable: no mutating request was sent.
    unknown: a mutating request may have taken effect; reconcile before any further write.
    """

    def __init__(self, code: str, outcome: Literal["rejected", "unavailable", "unknown"] = "rejected",
                 *, http_status: int | None = None, provider_code: str = ""):  # fmt: skip
        super().__init__(f"acuity:{code}:{outcome}")
        self.code, self.outcome = code, outcome
        self.http_status, self.provider_code = http_status, provider_code


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    user_id: str  # the Acuity account the owner connected; must match the stored credential


def _int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise AcuityError("invalid_id")
    return value


def _time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)  # Acuity: 2026-11-12T10:00:00-0500
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise AcuityError("invalid_timestamp") from None


class _Acuity:
    def __init__(self, http: httpx.AsyncClient):
        self.http, self.mutation_sent = http, False

    def malformed(self) -> AcuityError:
        return AcuityError("invalid_response", "unknown" if self.mutation_sent else "unavailable")

    async def request(self, method: str, path: str, *, params=None, body=None, mutate=False):
        if mutate:
            self.mutation_sent = True
        try:
            response = await self.http.request(method, API + path, params=params, json=body)
        except httpx.HTTPError:
            raise AcuityError("transport", "unknown" if mutate else "unavailable") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not 200 <= response.status_code < 300:
            code = payload.get("error") if isinstance(payload, dict) else ""
            code = code if isinstance(code, str) and code.replace("_", "").isalnum() else ""
            # 4xx proves nothing was written; timeouts and 5xx can follow an accepted write.
            unknown = mutate and (response.status_code >= 500 or response.status_code == 408)
            raise AcuityError("provider_error", "unknown" if unknown else ("rejected" if mutate else "unavailable"),
                              http_status=response.status_code, provider_code=code)  # fmt: skip
        if not isinstance(payload, (dict, list)):
            raise self.malformed()
        return payload


@asynccontextmanager
async def _operation(ws: str, binding: WorkspaceBinding):
    if not ws or binding.workspace_id != ws:
        raise AcuityError("workspace_mismatch")
    acuity = None
    try:
        async with asyncio.timeout(OPERATION_S), owner_lifecycle.owner_lifecycle_guard(ws):
            try:
                credential = await integration_secret_store.get(ws, "acuity")
            except Exception:  # noqa: BLE001 -- never leak credential errors
                raise AcuityError("credentials_unavailable", "unavailable") from None
            user_id, _, key = ((credential or {}).get("token") or "").partition(":")
            if not key:
                raise AcuityError("credentials_unavailable", "unavailable")
            if user_id != binding.user_id:
                raise AcuityError("account_mismatch")
            async with httpx.AsyncClient(timeout=HTTP_S, follow_redirects=False, auth=(user_id, key)) as http:
                acuity = _Acuity(http)
                yield acuity
    except (TimeoutError, asyncio.CancelledError) as exc:
        if acuity and acuity.mutation_sent:
            raise AcuityError("interrupted", "unknown") from None
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AcuityError("deadline", "unavailable") from None
    except owner_lifecycle.OwnerLifecycleBusyError:
        raise AcuityError("credentials_busy", "unavailable") from None
    except AcuityError:
        raise
    except Exception:  # noqa: BLE001
        raise AcuityError("operation_failed", "unknown" if acuity and acuity.mutation_sent else "unavailable") from None


def _list(acuity: _Acuity, answer: object) -> list[dict]:
    if not isinstance(answer, list) or not all(isinstance(r, dict) for r in answer):
        raise acuity.malformed()
    return answer


def _appointment(acuity: _Acuity, answer: object, appointment_id: int = 0) -> dict:
    try:
        if not isinstance(answer, dict):
            raise AcuityError("invalid_appointment")
        _int(answer.get("id")), _int(answer.get("appointmentTypeID")), _int(answer.get("calendarID"))
        _time(answer.get("datetime"))
        if appointment_id and answer["id"] != appointment_id:
            raise AcuityError("appointment_mismatch")
    except AcuityError as exc:
        if acuity.mutation_sent:
            raise acuity.malformed() from None
        raise exc
    return answer


async def appointment_types(ws: str, binding: WorkspaceBinding) -> list[dict]:
    """Our services: active, public types (id, name, duration minutes, price)."""
    async with _operation(ws, binding) as acuity:
        rows = _list(acuity, await acuity.request("GET", "/appointment-types"))
        return [r for r in rows if r.get("active") is not False and not r.get("private")]


async def calendars(ws: str, binding: WorkspaceBinding) -> list[dict]:
    """Our staff: one Acuity calendar per person (id, name)."""
    async with _operation(ws, binding) as acuity:
        return _list(acuity, await acuity.request("GET", "/calendars"))


async def times(ws: str, binding: WorkspaceBinding, *, date: str, appointment_type_id: int,
                calendar_id: int | None = None) -> list[str]:  # fmt: skip
    """Open start times on one day, Acuity's own offset-bearing strings, exactly as create() wants them."""
    params = {"date": date, "appointmentTypeID": _int(appointment_type_id)}
    if calendar_id is not None:
        params["calendarID"] = _int(calendar_id)
    async with _operation(ws, binding) as acuity:
        rows = _list(acuity, await acuity.request("GET", "/availability/times", params=params))
        out = []
        for row in rows:
            _time(row.get("time"))
            out.append(row["time"])
        return out


async def dates(ws: str, binding: WorkspaceBinding, *, month: str, appointment_type_id: int,
                calendar_id: int | None = None) -> list[str]:  # fmt: skip
    """Days in `month` (YYYY-MM) with any opening."""
    params = {"month": month, "appointmentTypeID": _int(appointment_type_id)}
    if calendar_id is not None:
        params["calendarID"] = _int(calendar_id)
    async with _operation(ws, binding) as acuity:
        rows = _list(acuity, await acuity.request("GET", "/availability/dates", params=params))
        if not all(isinstance(r.get("date"), str) for r in rows):
            raise acuity.malformed()
        return [r["date"] for r in rows]


async def get(ws: str, binding: WorkspaceBinding, appointment_id: int) -> dict:
    async with _operation(ws, binding) as acuity:
        return _appointment(
            acuity, await acuity.request("GET", f"/appointments/{_int(appointment_id)}"), appointment_id
        )


async def find(ws: str, binding: WorkspaceBinding, *, appointment_type_id: int, start: str, first_name: str,
               last_name: str, phone: str = "", calendar_id: int | None = None) -> dict | None:  # fmt: skip
    """Reconcile a lost create: the live (not canceled) appointment of this type, start and caller, or None.

    None after an unknown create is evidence only once the write cannot still be in flight."""
    when = _time(start)
    params = {"appointmentTypeID": _int(appointment_type_id), "canceled": "false", "firstName": first_name,
              "lastName": last_name, "minDate": (when - timedelta(days=1)).date().isoformat(),
              "maxDate": (when + timedelta(days=1)).date().isoformat()}  # fmt: skip
    if calendar_id is not None:
        params["calendarID"] = _int(calendar_id)
    async with _operation(ws, binding) as acuity:
        for row in _list(acuity, await acuity.request("GET", "/appointments", params=params)):
            try:
                same = _time(row.get("datetime")) == when
            except AcuityError:
                continue
            digits = lambda p: "".join(c for c in str(p or "") if c.isdigit())[-10:]  # noqa: E731
            if same and row.get("canceled") is not True and (not phone or digits(row.get("phone")) == digits(phone)):
                return _appointment(acuity, row)
        return None


async def create(ws: str, binding: WorkspaceBinding, *, appointment_type_id: int, start: str, first_name: str,
                 last_name: str, email: str, phone: str, calendar_id: int | None = None) -> dict:  # fmt: skip
    """Book as a client; Acuity re-checks the slot. email is mandatory in Acuity for non-admin booking."""
    when = _time(start)
    if not (first_name.strip() and last_name.strip() and phone.strip()):
        raise AcuityError("caller_details_required")
    if "@" not in email:
        raise AcuityError("email_required")
    body = {"appointmentTypeID": _int(appointment_type_id), "datetime": start, "firstName": first_name,
            "lastName": last_name, "email": email, "phone": phone}  # fmt: skip
    if calendar_id is not None:
        body["calendarID"] = _int(calendar_id)
    async with _operation(ws, binding) as acuity:
        result = _appointment(acuity, await acuity.request("POST", "/appointments", body=body, mutate=True))
        if _time(result["datetime"]) != when or result["appointmentTypeID"] != appointment_type_id:
            raise acuity.malformed()
        return result


async def reschedule(ws: str, binding: WorkspaceBinding, appointment_id: int, *, start: str) -> dict:
    when = _time(start)
    path = f"/appointments/{_int(appointment_id)}/reschedule"
    async with _operation(ws, binding) as acuity:
        _appointment(acuity, await acuity.request("GET", f"/appointments/{appointment_id}"), appointment_id)
        answer = await acuity.request("PUT", path, body={"datetime": start}, mutate=True)
        result = _appointment(acuity, answer, appointment_id)
        if _time(result["datetime"]) != when:
            raise acuity.malformed()
        return result


async def cancel(ws: str, binding: WorkspaceBinding, appointment_id: int) -> dict:
    path = f"/appointments/{_int(appointment_id)}/cancel"
    async with _operation(ws, binding) as acuity:
        _appointment(acuity, await acuity.request("GET", f"/appointments/{appointment_id}"), appointment_id)
        result = _appointment(acuity, await acuity.request("PUT", path, mutate=True), appointment_id)
        if result.get("canceled") is not True:
            raise acuity.malformed()
        return result
