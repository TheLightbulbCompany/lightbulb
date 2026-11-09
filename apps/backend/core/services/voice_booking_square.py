"""Bounded Square seller-booking adapter; not yet connected to voice_booking.

The canonical booking owner must supply authenticated workspace/contact bindings,
authorize the effect, and persist an immutable operation ID + payload BEFORE calling.
It owns holds, reconciliation and caller-visible outcomes. No retries or local effects
live here. In particular, SquareError(outcome="unknown") must never become a confirmed
booking, a successful cancellation, or a new attempt with a new operation ID.

Source contracts (checked 2026-11-09):
https://developer.squareup.com/docs/bookings-api/what-it-is
https://developer.squareup.com/docs/bookings-api/use-the-api
https://developer.squareup.com/reference/square/bookings/search-availability
https://developer.squareup.com/reference/square/bookings-api/create-booking
https://developer.squareup.com/reference/square/bookings-api/update-booking
https://developer.squareup.com/reference/square/bookings-api/CancelBooking
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import quote

import httpx

from core.services import owner_lifecycle
from core.services.credentials import access_token, integration_secret_store

API = "https://connect.squareup.com"
VERSION = "2026-09-16"
HTTP_S = 2.0
OPERATION_S = 12.0  # includes credentials, preflight and HTTP; not a measured voice latency claim
READ_SCOPES = frozenset({"APPOINTMENTS_READ", "APPOINTMENTS_ALL_READ"})
WRITE_SCOPES = frozenset({"APPOINTMENTS_WRITE", "APPOINTMENTS_ALL_WRITE", "CUSTOMERS_READ"})
SETTINGS_SCOPES = frozenset({"APPOINTMENTS_BUSINESS_SETTINGS_READ", "MERCHANT_PROFILE_READ"})


class SquareError(RuntimeError):
    """Safe to pass to the canonical owner; never contains provider detail/body or tokens.

    rejected: no write was accepted. unavailable: no mutating request was sent.
    unknown: a mutating request may have taken effect; reconcile before any further write.
    """

    def __init__(
        self,
        code: str,
        outcome: Literal["rejected", "unavailable", "unknown"] = "rejected",
        *,
        http_status: int | None = None,
        provider_codes: tuple[str, ...] = (),
    ):
        super().__init__(f"square:{code}:{outcome}")
        self.code, self.outcome = code, outcome
        self.http_status, self.provider_codes = http_status, provider_codes


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_id: str
    merchant_id: str
    location_id: str


@dataclass(frozen=True)
class CustomerBinding:
    """From the workspace's canonical contact mapping, never a caller-supplied Square ID."""

    workspace_id: str
    merchant_id: str
    customer_id: str


@dataclass(frozen=True)
class Readiness:
    seller_writes: bool
    online_booking: bool
    missing_scopes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.seller_writes and not self.missing_scopes


def _id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SquareError("invalid_binding")
    return quote(value, safe="")


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise SquareError("invalid_timestamp") from None


def _version(value: int) -> None:
    if type(value) is not int or value < 0:
        raise SquareError("invalid_version")


def _key(ws: str, binding: WorkspaceBinding, action: str, operation_id: str, booking_id: str = "") -> str:
    _id(operation_id)
    # No timestamps, random UUIDs, or payload hashes: the SAME canonical operation
    # keeps its key, even after a timeout. Square refuses key reuse with a changed body.
    canonical = ["lightbulb-square-v1", ws, binding.merchant_id, binding.location_id, action, booking_id, operation_id]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def _segments(value: object) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise SquareError("invalid_segments")
    for segment in value:
        if not isinstance(segment, dict):
            raise SquareError("invalid_segments")
        for key in ("team_member_id", "service_variation_id"):
            _id(segment.get(key))
        _version(segment.get("service_variation_version"))
        if type(segment.get("duration_minutes")) is not int or segment["duration_minutes"] <= 0:
            raise SquareError("invalid_segments")
    return deepcopy(value)


class _Square:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.mutation_sent = False
        self.scopes: set[str] = set()

    def malformed(self) -> SquareError:
        return SquareError("invalid_response", "unknown" if self.mutation_sent else "unavailable")

    async def request(self, method: str, path: str, body: dict | None = None, *, mutate: bool = False) -> dict:
        if mutate:
            self.mutation_sent = True
        try:
            response = await self.http.request(method, API + path, json=body)
        except httpx.HTTPError:
            raise SquareError("transport", "unknown" if mutate else "unavailable") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors is not None and not isinstance(errors, list):
            raise self.malformed()
        if not 200 <= response.status_code < 300 or errors:
            codes = (
                tuple(
                    e["code"]
                    for e in errors or []
                    if isinstance(e, dict)
                    and isinstance(e.get("code"), str)
                    and re.fullmatch(r"[A-Z_]{1,80}", e["code"])
                )
                if isinstance(errors, list)
                else ()
            )
            # Timeouts and server errors can follow an accepted write. Any
            # error-bearing 2xx is inconsistent and cannot prove no write occurred.
            unknown = mutate and (
                response.status_code >= 500
                or response.status_code == 408
                or 200 <= response.status_code < 300
                or (isinstance(payload, dict) and bool(payload.get("booking")))
            )
            raise SquareError(
                "provider_error",
                "unknown" if unknown else ("rejected" if mutate else "unavailable"),
                http_status=response.status_code,
                provider_codes=codes,
            )
        if not isinstance(payload, dict):
            raise self.malformed()
        return payload


@asynccontextmanager
async def _operation(ws: str, binding: WorkspaceBinding, customer: CustomerBinding | None = None):
    _id(ws)
    if binding.workspace_id != ws:
        raise SquareError("workspace_mismatch")
    _id(binding.merchant_id)
    _id(binding.location_id)
    if customer:
        if (customer.workspace_id, customer.merchant_id) != (ws, binding.merchant_id):
            raise SquareError("customer_binding_mismatch")
        _id(customer.customer_id)
    square = None
    try:
        async with asyncio.timeout(OPERATION_S), owner_lifecycle.owner_lifecycle_guard(ws):
            # Reentrant lifecycle lease: token + merchant are one credential snapshot,
            # and a reconnect/disconnect cannot swap sellers during the operation.
            try:
                token = await access_token.get(ws, "square")
                credential = await integration_secret_store.get(ws, "square")
            except Exception:  # noqa: BLE001 -- no provider write was sent; never leak credential errors
                raise SquareError("credentials_unavailable", "unavailable") from None
            if not credential or credential.get("merchant_id") != binding.merchant_id:
                raise SquareError("merchant_mismatch")
            async with httpx.AsyncClient(
                timeout=HTTP_S,
                follow_redirects=False,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Square-Version": VERSION,
                    "Content-Type": "application/json",
                },
            ) as http:
                square = _Square(http)
                status = await square.request("POST", "/oauth2/token/status")  # read-only POST
                if status.get("merchant_id") != binding.merchant_id:
                    raise SquareError("merchant_mismatch")
                scopes = status.get("scopes")
                if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                    raise square.malformed()
                square.scopes = set(scopes)
                if "MERCHANT_PROFILE_READ" not in square.scopes:
                    raise SquareError("missing_scope")
                location = (await square.request("GET", f"/v2/locations/{_id(binding.location_id)}")).get("location")
                if not isinstance(location, dict):
                    raise square.malformed()
                if (location.get("id"), location.get("merchant_id")) != (binding.location_id, binding.merchant_id):
                    raise SquareError("location_mismatch")
                if location.get("status") != "ACTIVE":
                    raise SquareError("location_inactive")
                yield square
    except (TimeoutError, asyncio.CancelledError) as exc:
        if square and square.mutation_sent:
            raise SquareError("interrupted", "unknown") from None
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SquareError("deadline", "unavailable") from None
    except owner_lifecycle.OwnerLifecycleBusyError:
        raise SquareError("credentials_busy", "unavailable") from None
    except SquareError:
        raise
    except Exception:  # noqa: BLE001 -- includes lease teardown failing after a dispatched write
        raise SquareError("operation_failed", "unknown" if square and square.mutation_sent else "unavailable") from None


async def _readiness(square: _Square, binding: WorkspaceBinding) -> Readiness:
    if "APPOINTMENTS_BUSINESS_SETTINGS_READ" not in square.scopes:
        raise SquareError("missing_scope")
    business = (await square.request("GET", "/v2/bookings/business-booking-profile")).get("business_booking_profile")
    location = (await square.request("GET", f"/v2/bookings/location-booking-profiles/{_id(binding.location_id)}")).get(
        "location_booking_profile"
    )
    if not isinstance(business, dict) or not isinstance(location, dict):
        raise square.malformed()
    if business.get("seller_id") != binding.merchant_id or location.get("location_id") != binding.location_id:
        raise SquareError("profile_binding_mismatch")
    missing = tuple(sorted((READ_SCOPES | WRITE_SCOPES | SETTINGS_SCOPES) - square.scopes))
    return Readiness(
        business.get("support_seller_level_writes") is True, location.get("online_booking_enabled") is True, missing
    )


async def readiness(ws: str, binding: WorkspaceBinding) -> Readiness:
    """Paid Appointments seller-write capability, online availability flag, and missing scopes.

    online_booking is separate: seller writes do not require online booking enabled.
    Missing/false support_seller_level_writes is never promoted to ready.
    """
    async with _operation(ws, binding) as square:
        return await _readiness(square, binding)


async def availability(
    ws: str, binding: WorkspaceBinding, *, start_at: str, end_at: str, segment_filters: list[dict]
) -> list[dict]:
    """Square's buyer-accessible slots, full segments and catalog versions intact; no fallback."""
    if not timedelta(days=1) <= _time(end_at) - _time(start_at) <= timedelta(days=31):
        raise SquareError("invalid_search_range")
    if not isinstance(segment_filters, list) or not segment_filters:
        raise SquareError("invalid_filters")
    for segment in segment_filters:
        if not isinstance(segment, dict) or set(segment) - {"service_variation_id", "team_member_id_filter"}:
            raise SquareError("invalid_filters")
        _id(segment.get("service_variation_id"))
        if "team_member_id_filter" in segment:
            members = segment["team_member_id_filter"]
            if (
                not isinstance(members, dict)
                or set(members) != {"any"}
                or not isinstance(members["any"], list)
                or not members["any"]
            ):
                raise SquareError("invalid_filters")
            for member in members["any"]:
                _id(member)
    async with _operation(ws, binding) as square:
        if READ_SCOPES - square.scopes:
            raise SquareError("missing_scope")
        answer = await square.request(
            "POST",
            "/v2/bookings/availability/search",
            {
                "query": {
                    "filter": {
                        "start_at_range": {"start_at": start_at, "end_at": end_at},
                        "location_id": binding.location_id,
                        "segment_filters": deepcopy(segment_filters),
                    }
                }
            },
        )
        slots = answer.get("availabilities", [])  # Square may omit an empty repeated field
        if not isinstance(slots, list):
            raise square.malformed()
        try:
            for slot in slots:
                if not isinstance(slot, dict) or slot.get("location_id") != binding.location_id:
                    raise SquareError("location_mismatch")
                _time(slot.get("start_at"))
                _segments(slot.get("appointment_segments"))
        except SquareError:
            raise square.malformed() from None
        return deepcopy(slots)


def _booking(
    square: _Square, answer: dict, binding: WorkspaceBinding, customer: CustomerBinding, booking_id: str = ""
) -> dict:
    booking = answer.get("booking")
    try:
        if not isinstance(booking, dict):
            raise SquareError("invalid_booking")
        _id(booking.get("id"))
        _version(booking.get("version"))
        _time(booking.get("start_at"))
        _segments(booking.get("appointment_segments"))
        if (booking.get("location_id"), booking.get("customer_id")) != (binding.location_id, customer.customer_id):
            raise SquareError("booking_binding_mismatch")
        if booking_id and booking.get("id") != booking_id:
            raise SquareError("booking_binding_mismatch")
        if not isinstance(booking.get("status"), str) or not booking["status"]:
            raise SquareError("invalid_booking")
    except SquareError as exc:
        if square.mutation_sent:
            raise square.malformed() from None
        raise exc
    return deepcopy(booking)


async def get(ws: str, binding: WorkspaceBinding, customer: CustomerBinding, booking_id: str) -> dict:
    path = f"/v2/bookings/{_id(booking_id)}"
    async with _operation(ws, binding, customer) as square:
        if READ_SCOPES - square.scopes:
            raise SquareError("missing_scope")
        return _booking(square, await square.request("GET", path), binding, customer, booking_id)


async def _write_ready(
    square: _Square, binding: WorkspaceBinding, customer: CustomerBinding, *, require_phone: bool = False
) -> None:
    if not (await _readiness(square, binding)).ready:
        raise SquareError("seller_writes_not_ready")
    found = (await square.request("GET", f"/v2/customers/{_id(customer.customer_id)}")).get("customer")
    if not isinstance(found, dict) or found.get("id") != customer.customer_id:
        raise SquareError("customer_mismatch")
    if require_phone and not found.get("phone_number"):
        raise SquareError("customer_phone_required")


async def create(
    ws: str, binding: WorkspaceBinding, customer: CustomerBinding, slot: dict, *, operation_id: str
) -> dict:
    key = _key(ws, binding, "create", operation_id)
    if slot.get("location_id") != binding.location_id:
        raise SquareError("location_mismatch")
    _time(slot.get("start_at"))
    segments = _segments(slot.get("appointment_segments"))
    # Every segment is retained. Only Square's writable segment fields go into
    # CreateBooking; read-only intermission/resource fields stay in read results.
    fields = ("duration_minutes", "team_member_id", "service_variation_id", "service_variation_version")
    booking = {
        "location_id": binding.location_id,
        "customer_id": customer.customer_id,
        "start_at": slot["start_at"],
        "appointment_segments": [{k: s[k] for k in fields} for s in segments],
    }
    async with _operation(ws, binding, customer) as square:
        await _write_ready(square, binding, customer, require_phone=True)
        answer = await square.request("POST", "/v2/bookings", {"idempotency_key": key, "booking": booking}, mutate=True)
        result = _booking(square, answer, binding, customer)
        if _time(result["start_at"]) != _time(booking["start_at"]):
            raise square.malformed()
        return result


async def update(
    ws: str,
    binding: WorkspaceBinding,
    customer: CustomerBinding,
    booking_id: str,
    *,
    version: int,
    start_at: str,
    operation_id: str,
) -> dict:
    """Bounded move, using the caller's observed booking version; never refresh-and-retry.

    Sparse start_at update preserves the provider's ENTIRE segment array, including
    read-only fields. Never reconstruct a booking from just its first service.
    """
    path = f"/v2/bookings/{_id(booking_id)}"
    _version(version)
    _time(start_at)
    key = _key(ws, binding, "update", operation_id, booking_id)
    async with _operation(ws, binding, customer) as square:
        await _write_ready(square, binding, customer)
        _booking(square, await square.request("GET", path), binding, customer, booking_id)
        # Square arbitrates the supplied version AND idempotency key. Do not
        # reject a replay locally just because its first attempt advanced version.
        answer = await square.request(
            "PUT",
            path,
            {
                "idempotency_key": key,
                "booking": {"version": version, "start_at": start_at},
            },
            mutate=True,
        )
        result = _booking(square, answer, binding, customer, booking_id)
        if result["version"] <= version or _time(result["start_at"]) != _time(start_at):
            raise square.malformed()
        return result


async def cancel(
    ws: str,
    binding: WorkspaceBinding,
    customer: CustomerBinding,
    booking_id: str,
    *,
    version: int,
    operation_id: str,
) -> dict:
    path = f"/v2/bookings/{_id(booking_id)}"
    _version(version)
    key = _key(ws, binding, "cancel", operation_id, booking_id)
    async with _operation(ws, binding, customer) as square:
        await _write_ready(square, binding, customer)
        _booking(square, await square.request("GET", path), binding, customer, booking_id)
        answer = await square.request(
            "POST",
            path + "/cancel",
            {
                "idempotency_key": key,
                "booking_version": version,
            },
            mutate=True,
        )
        booking = _booking(square, answer, binding, customer, booking_id)
        if booking["version"] <= version or booking["status"] not in ("CANCELLED_BY_SELLER", "CANCELLED_BY_CUSTOMER"):
            raise square.malformed()
        return booking
