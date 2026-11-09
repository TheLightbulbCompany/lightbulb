"""Mocked Square wire contracts; these do not establish provider acceptance."""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import voice_booking_square as sq

WS = "org_business"
BINDING = sq.WorkspaceBinding(WS, "merchant_1", "location_1")
CUSTOMER = sq.CustomerBinding(WS, "merchant_1", "customer_1")
START = "2026-12-01T10:00:00Z"
END = "2026-12-02T10:00:00Z"
SEGMENTS = [
    {
        "duration_minutes": 30,
        "team_member_id": "staff_1",
        "service_variation_id": "variation_1",
        "service_variation_version": 1890000000123,
        "intermission_minutes": 5,
        "resource_ids": ["chair_1"],
    },
    {
        "duration_minutes": 15,
        "team_member_id": "staff_2",
        "service_variation_id": "variation_2",
        "service_variation_version": 1890000000456,
    },
]
SLOT = {"location_id": "location_1", "start_at": START, "appointment_segments": SEGMENTS}
BOOKING = {**SLOT, "id": "booking_1", "version": 7, "status": "ACCEPTED", "customer_id": "customer_1"}
FILTERS = [
    {"service_variation_id": "variation_1", "team_member_id_filter": {"any": ["staff_1"]}},
    {"service_variation_id": "variation_2", "team_member_id_filter": {"any": ["staff_2"]}},
]


@pytest.fixture
def wire(monkeypatch):
    original_client = httpx.AsyncClient
    state = {
        "credential": {"access_token": "fake-token", "merchant_id": "merchant_1"},
        "status": {
            "merchant_id": "merchant_1",
            "scopes": sorted(sq.READ_SCOPES | sq.WRITE_SCOPES | sq.SETTINGS_SCOPES),
        },
        "location": {"id": "location_1", "merchant_id": "merchant_1", "status": "ACTIVE"},
        "business": {"seller_id": "merchant_1", "support_seller_level_writes": True},
        "location_profile": {"location_id": "location_1", "online_booking_enabled": True},
        "customer": {"id": "customer_1", "phone_number": "+15555550123"},
        "booking": deepcopy(BOOKING),
        "slots": [deepcopy(SLOT)],
        "requests": [],
        "overrides": {},
        "leases": [],
    }

    @asynccontextmanager
    async def guard(ws):
        assert ws == WS
        state["leases"].append(ws)
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    async def token(ws, provider):
        assert state["locked"] and (ws, provider) == (WS, "square")
        return "fake-token"

    async def credential(ws, provider):
        assert state["locked"] and (ws, provider) == (WS, "square")
        return state["credential"]

    async def handle(request):
        assert state["locked"]
        assert request.url.host == "connect.squareup.com"
        assert request.headers["Authorization"] == "Bearer fake-token"
        assert request.headers["Square-Version"] == "2026-09-16"
        assert request.headers["Content-Type"] == "application/json"
        assert "fake-token" not in str(request.url)
        state["requests"].append(request)
        method, path = request.method, request.url.path
        if override := state["overrides"].get((method, path)):
            if callable(override):
                return await override(request)
            return override
        if path == "/oauth2/token/status":
            assert method == "POST"
            body = state["status"]
        elif path == "/v2/locations/location_1":
            assert method == "GET"
            body = {"location": state["location"]}
        elif path == "/v2/bookings/business-booking-profile":
            body = {"business_booking_profile": state["business"]}
        elif path == "/v2/bookings/location-booking-profiles/location_1":
            body = {"location_booking_profile": state["location_profile"]}
        elif path == "/v2/customers/customer_1":
            body = {"customer": state["customer"]}
        elif path == "/v2/bookings/availability/search":
            assert method == "POST"
            body = {"availabilities": state["slots"]}
        elif (method, path) == ("POST", "/v2/bookings"):
            body = {"booking": state["booking"]}
        elif path == "/v2/bookings/booking_1/cancel":
            assert method == "POST"
            body = {"booking": {**state["booking"], "version": 8, "status": "CANCELLED_BY_SELLER"}}
        elif path == "/v2/bookings/booking_1":
            assert method in ("GET", "PUT")
            booking = deepcopy(state["booking"])
            if method == "PUT":
                booking.update(json.loads(request.content)["booking"])
                booking["version"] += 1
            body = {"booking": booking}
        else:
            raise AssertionError((method, path))
        return httpx.Response(200, json=body)

    monkeypatch.setattr(sq.owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(sq.access_token, "get", AsyncMock(side_effect=token))
    monkeypatch.setattr(sq.integration_secret_store, "get", AsyncMock(side_effect=credential))
    monkeypatch.setattr(
        sq.httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw)
    )
    return state


def writes(wire):
    return [
        r
        for r in wire["requests"]
        if r.url.path == "/v2/bookings" or r.method == "PUT" or r.url.path.endswith("/cancel")
    ]


async def create():
    return await sq.create(WS, BINDING, CUSTOMER, deepcopy(SLOT), operation_id="canonical-create-1")


async def update():
    return await sq.update(WS, BINDING, CUSTOMER, "booking_1", version=7, start_at=END, operation_id="canonical-move-1")


async def cancel():
    return await sq.cancel(WS, BINDING, CUSTOMER, "booking_1", version=7, operation_id="canonical-cancel-1")


async def test_readiness_checks_capability_location_and_granted_scopes(wire):
    ready = await sq.readiness(WS, BINDING)
    assert ready.ready and ready.seller_writes and ready.online_booking
    assert not writes(wire)
    assert wire["leases"] == [WS] and not wire["locked"]
    assert [r.url.path for r in wire["requests"]] == [
        "/oauth2/token/status",
        "/v2/locations/location_1",
        "/v2/bookings/business-booking-profile",
        "/v2/bookings/location-booking-profiles/location_1",
    ]


@pytest.mark.parametrize("support", [False, None, "true", 1])
async def test_no_paid_capability_never_falls_back_to_buyer_write(wire, support):
    wire["business"]["support_seller_level_writes"] = support
    assert not (await sq.readiness(WS, BINDING)).ready
    with pytest.raises(sq.SquareError, match="seller_writes_not_ready"):
        await create()
    assert not writes(wire)


async def test_online_disabled_is_separate_from_seller_write_capability(wire):
    wire["location_profile"]["online_booking_enabled"] = False
    ready = await sq.readiness(WS, BINDING)
    assert ready.ready and not ready.online_booking


async def test_missing_seller_scope_does_not_claim_ready(wire):
    wire["status"]["scopes"].remove("APPOINTMENTS_ALL_WRITE")
    ready = await sq.readiness(WS, BINDING)
    assert not ready.ready and ready.missing_scopes == ("APPOINTMENTS_ALL_WRITE",)
    with pytest.raises(sq.SquareError):
        await create()
    assert not writes(wire)


async def test_availability_wire_contract_and_every_segment_preserved(wire):
    result = await sq.availability(WS, BINDING, start_at=START, end_at=END, segment_filters=FILTERS)
    assert result == [SLOT] and result[0]["appointment_segments"] == SEGMENTS
    assert json.loads(wire["requests"][-1].content) == {
        "query": {
            "filter": {
                "location_id": "location_1",
                "start_at_range": {"start_at": START, "end_at": END},
                "segment_filters": FILTERS,
            }
        }
    }
    result[0]["appointment_segments"][0]["duration_minutes"] = 999
    assert wire["slots"][0]["appointment_segments"][0]["duration_minutes"] == 30


@pytest.mark.parametrize("end", ["2026-12-02T09:59:59Z", "2027-01-01T10:00:01Z", "2026-12-02T10:00:00"])
async def test_invalid_availability_windows_do_not_reach_provider(wire, end):
    with pytest.raises(sq.SquareError):
        await sq.availability(WS, BINDING, start_at=START, end_at=end, segment_filters=FILTERS)
    assert not wire["requests"]


async def test_unavailable_is_distinct_from_empty_availability(wire):
    wire["slots"] = []
    assert await sq.availability(WS, BINDING, start_at=START, end_at=END, segment_filters=FILTERS) == []
    wire["overrides"][("POST", "/v2/bookings/availability/search")] = httpx.Response(503, json={})
    with pytest.raises(sq.SquareError) as err:
        await sq.availability(WS, BINDING, start_at=START, end_at=END, segment_filters=FILTERS)
    assert err.value.outcome == "unavailable"


async def test_create_uses_customer_and_catalog_versions_with_stable_key(wire):
    selected = deepcopy(SLOT)
    assert await sq.create(WS, BINDING, CUSTOMER, selected, operation_id="canonical-create-1") == BOOKING
    assert selected == SLOT
    await create()
    first, second = [json.loads(r.content) for r in writes(wire)]
    assert first == second and len(first["idempotency_key"]) == 64
    assert first["booking"]["customer_id"] == "customer_1"
    assert first["booking"]["appointment_segments"] == [
        {k: s[k] for k in ("duration_minutes", "team_member_id", "service_variation_id", "service_variation_version")}
        for s in SEGMENTS
    ]


async def test_get_returns_full_booking_and_sparse_move_preserves_segments(wire):
    assert await sq.get(WS, BINDING, CUSTOMER, "booking_1") == BOOKING
    moved = await update()
    request = json.loads(writes(wire)[0].content)
    assert request["booking"] == {"version": 7, "start_at": END}
    assert moved["version"] == 8 and moved["appointment_segments"] == SEGMENTS
    assert moved["start_at"] == END


async def test_cancel_wire_contract_and_confirmed_status(wire):
    cancelled = await cancel()
    assert cancelled["status"] == "CANCELLED_BY_SELLER" and cancelled["version"] == 8
    assert json.loads(writes(wire)[0].content)["booking_version"] == 7
    assert cancelled["appointment_segments"] == SEGMENTS


async def test_keys_are_scoped_by_workspace_merchant_location_action_and_target(wire):
    key = sq._key(WS, BINDING, "create", "op")
    assert key == sq._key(WS, BINDING, "create", "op")
    assert (
        len(
            {
                key,
                sq._key("org_other", BINDING, "create", "op"),
                sq._key(WS, replace(BINDING, merchant_id="merchant_2"), "create", "op"),
                sq._key(WS, replace(BINDING, location_id="location_2"), "create", "op"),
                sq._key(WS, BINDING, "update", "op", "booking_1"),
                sq._key(WS, BINDING, "update", "op", "booking_2"),
            }
        )
        == 6
    )


@pytest.mark.parametrize(
    "binding,customer",
    [
        (replace(BINDING, workspace_id="org_other"), CUSTOMER),
        (BINDING, replace(CUSTOMER, workspace_id="org_other")),
        (BINDING, replace(CUSTOMER, merchant_id="merchant_2")),
    ],
)
async def test_foreign_workspace_or_customer_never_reads_credentials_or_http(wire, binding, customer):
    with pytest.raises(sq.SquareError):
        await sq.create(WS, binding, customer, SLOT, operation_id="op")
    sq.access_token.get.assert_not_awaited()
    assert not wire["requests"]


@pytest.mark.parametrize(
    "source,field,value",
    [
        ("credential", "merchant_id", "merchant_2"),
        ("status", "merchant_id", "merchant_2"),
        ("location", "merchant_id", "merchant_2"),
        ("location", "id", "location_2"),
        ("location", "status", "INACTIVE"),
        ("business", "seller_id", "merchant_2"),
        ("location_profile", "location_id", "location_2"),
        ("customer", "id", "customer_2"),
        ("customer", "phone_number", ""),
    ],
)
async def test_binding_and_readiness_mismatches_prevent_mutation(wire, source, field, value):
    wire[source][field] = value
    with pytest.raises(sq.SquareError):
        await create()
    assert not writes(wire)


@pytest.mark.parametrize(
    "field,value", [("customer_id", "customer_2"), ("location_id", "location_2"), ("id", "booking_2")]
)
@pytest.mark.parametrize("action", [update, cancel])
async def test_foreign_booking_prevents_move_cancel(wire, field, value, action):
    wire["booking"][field] = value
    with pytest.raises(sq.SquareError):
        await action()
    assert not writes(wire)


@pytest.mark.parametrize(
    "action,method,path",
    [
        (create, "POST", "/v2/bookings"),
        (update, "PUT", "/v2/bookings/booking_1"),
        (cancel, "POST", "/v2/bookings/booking_1/cancel"),
    ],
)
@pytest.mark.parametrize(
    "status,code,outcome",
    [
        (401, "UNAUTHORIZED", "rejected"),
        (403, "FORBIDDEN", "rejected"),
        (404, "NOT_FOUND", "rejected"),
        (409, "VERSION_MISMATCH", "rejected"),
        (429, "RATE_LIMITED", "rejected"),
        (500, "INTERNAL_SERVER_ERROR", "unknown"),
        (408, "REQUEST_TIMEOUT", "unknown"),
    ],
)
async def test_provider_errors_are_explicit_and_never_retried(wire, action, method, path, status, code, outcome):
    wire["overrides"][(method, path)] = httpx.Response(
        status,
        json={
            "errors": [
                {
                    "code": code,
                    "detail": "secret-echo fake-token",
                    "category": "INVALID_REQUEST_ERROR",
                }
            ]
        },
    )
    with pytest.raises(sq.SquareError) as err:
        await action()
    assert (err.value.outcome, err.value.http_status, err.value.provider_codes) == (outcome, status, (code,))
    assert "fake-token" not in str(err.value) and "secret-echo" not in str(err.value)
    assert len(writes(wire)) == 1


@pytest.mark.parametrize(
    "action,method,path",
    [
        (create, "POST", "/v2/bookings"),
        (update, "PUT", "/v2/bookings/booking_1"),
        (cancel, "POST", "/v2/bookings/booking_1/cancel"),
    ],
)
@pytest.mark.parametrize(
    "failure", ["timeout", "disconnect", "bad_json", "missing_booking", "wrong_customer", "errors_200"]
)
async def test_ambiguous_mutation_never_masquerades_as_success(wire, action, method, path, failure):
    async def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("fake-token")
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("fake-token")
        if failure == "bad_json":
            return httpx.Response(200, text="not JSON")
        if failure == "missing_booking":
            return httpx.Response(200, json={})
        if failure == "wrong_customer":
            return httpx.Response(200, json={"booking": {**BOOKING, "customer_id": "customer_2"}})
        return httpx.Response(200, json={"booking": BOOKING, "errors": [{"code": "INTERNAL_SERVER_ERROR"}]})

    wire["overrides"][(method, path)] = fail
    with pytest.raises(sq.SquareError) as err:
        await action()
    assert err.value.outcome == "unknown" and len(writes(wire)) == 1


async def test_deadline_during_write_is_unknown_and_same_operation_keeps_key(wire, monkeypatch):
    async def hang(request):
        await asyncio.Event().wait()

    wire["overrides"][("POST", "/v2/bookings")] = hang
    monkeypatch.setattr(sq, "OPERATION_S", 0.05)
    with pytest.raises(sq.SquareError) as err:
        await create()
    assert err.value.outcome == "unknown" and not wire["locked"]
    first_key = json.loads(writes(wire)[0].content)["idempotency_key"]
    wire["overrides"].clear()
    monkeypatch.setattr(sq, "OPERATION_S", 12)
    await create()
    assert json.loads(writes(wire)[1].content)["idempotency_key"] == first_key


async def test_read_deadline_is_unavailable_and_does_not_write(wire, monkeypatch):
    async def hang(request):
        await asyncio.Event().wait()

    wire["overrides"][("POST", "/oauth2/token/status")] = hang
    monkeypatch.setattr(sq, "OPERATION_S", 0.05)
    with pytest.raises(sq.SquareError) as err:
        await create()
    assert err.value.outcome == "unavailable" and not writes(wire)


async def test_cancel_requires_cancelled_response_not_http_200_alone(wire):
    wire["overrides"][("POST", "/v2/bookings/booking_1/cancel")] = httpx.Response(200, json={"booking": BOOKING})
    with pytest.raises(sq.SquareError) as err:
        await cancel()
    assert err.value.outcome == "unknown"


async def test_missing_grant_is_unavailable_without_provider_call(wire):
    sq.access_token.get.side_effect = sq.access_token.ReconnectRequired("square")
    with pytest.raises(sq.SquareError) as err:
        await create()
    assert err.value.code == "credentials_unavailable" and err.value.outcome == "unavailable"
    assert not wire["requests"]


async def test_move_keeps_original_version_when_preflight_observes_newer_version(wire):
    # A replay's earlier attempt may already have advanced the booking. Square's
    # idempotency lookup must receive the ORIGINAL version, never an auto-refreshed one.
    wire["booking"]["version"] = 8
    await update()
    body = json.loads(writes(wire)[0].content)
    assert body["booking"]["version"] == 7


async def test_payload_change_cannot_escape_provider_idempotency_conflict(wire):
    seen = {}

    async def idempotent(request):
        body = json.loads(request.content)
        key = body["idempotency_key"]
        if key in seen and seen[key] != body:
            return httpx.Response(400, json={"errors": [{"code": "IDEMPOTENCY_KEY_REUSED"}]})
        seen[key] = body
        return httpx.Response(200, json={"booking": {**BOOKING, "start_at": body["booking"]["start_at"]}})

    wire["overrides"][("POST", "/v2/bookings")] = idempotent
    await create()
    with pytest.raises(sq.SquareError) as err:
        await sq.create(WS, BINDING, CUSTOMER, {**SLOT, "start_at": END}, operation_id="canonical-create-1")
    assert err.value.outcome == "rejected" and err.value.provider_codes == ("IDEMPOTENCY_KEY_REUSED",)
    assert len(seen) == 1 and len(writes(wire)) == 2


async def test_same_slot_new_operation_gets_distinct_key(wire):
    await create()
    await sq.create(WS, BINDING, CUSTOMER, SLOT, operation_id="canonical-create-2")
    assert len({json.loads(r.content)["idempotency_key"] for r in writes(wire)}) == 2


async def test_failed_lease_release_after_write_exposes_unknown(wire, monkeypatch):
    @asynccontextmanager
    async def broken_guard(ws):
        wire["locked"] = True
        try:
            yield
        finally:
            wire["locked"] = False
            raise RuntimeError("lease release failure")

    monkeypatch.setattr(sq.owner_lifecycle, "owner_lifecycle_guard", broken_guard)
    with pytest.raises(sq.SquareError) as err:
        await create()
    assert err.value.outcome == "unknown" and len(writes(wire)) == 1


async def test_cancellation_after_dispatch_is_unknown(wire):
    async def cancelled(request):
        raise asyncio.CancelledError

    wire["overrides"][("POST", "/v2/bookings")] = cancelled
    with pytest.raises(sq.SquareError) as err:
        await create()
    assert err.value.outcome == "unknown" and len(writes(wire)) == 1


@pytest.mark.parametrize("field,value", [("start_at", START), ("version", 7)])
async def test_move_requires_returned_time_and_advanced_version(wire, field, value):
    booking = {**BOOKING, "start_at": END, "version": 8, field: value}
    wire["overrides"][("PUT", "/v2/bookings/booking_1")] = httpx.Response(200, json={"booking": booking})
    with pytest.raises(sq.SquareError) as err:
        await update()
    assert err.value.outcome == "unknown"


@pytest.mark.parametrize("action", [update, cancel])
async def test_existing_booking_can_move_or_cancel_after_customer_phone_removed(wire, action):
    wire["customer"].pop("phone_number")
    assert (await action())["version"] > 7
