"""Ask to pick up (spec 2026-10-05-ask-to-pick-up): a teammate asks, the owner allows or declines."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_pickup as pickup
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import start
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER
from tests.unit.services.test_session_pickup import claude_session

db = registry_tests.db
transport = relay_tests.transport

CAROL = AuthContext(user_id="user_carol", org_id=OWNER)
NAMES = {ALICE.user_id: "Alice", BOB.user_id: "Bob", CAROL.user_id: "Carol"}


@contextmanager
def recorded():
    """Notices, activity rows and presence pushes, captured instead of sent."""
    log = {"notices": [], "activity": [], "presence": []}

    async def notice(owner, user, sid, state, title, body, event_id):
        log["notices"].append({"user": user, "state": state, "title": title, "body": body, "eventId": event_id})

    async def activity(row):
        log["activity"].append(row)

    async def presence(row):
        log["presence"].append(row)

    async def name(user_id):
        return NAMES.get(user_id, user_id)

    with (
        patch.object(service, "_session_notice", notice),
        patch.object(messaging_repo, "put_activity_row", activity),
        patch.object(service, "_push_presence", presence),
        patch("core.services.messaging.human_display_name", name),
    ):
        yield log


async def stored(sid) -> dict:
    return await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_a_teammate_asks_once_and_only_the_owner_hears(transport):
    sid = await claude_session(transport)
    with recorded() as log:
        public = await service.request_pickup_access(BOB, sid)
        assert public.pickup_request == "pending"
        first = (await stored(sid))["pickupRequests"][BOB.user_id]
        assert first["state"] == "pending" and first["requestId"]
        revision = (await stored(sid))["revision"]
        again = await service.request_pickup_access(BOB, sid)
    assert again.pickup_request == "pending"
    assert (await stored(sid))["revision"] == revision  # no second write
    assert [(n["user"], n["state"], n["title"]) for n in log["notices"]] == [
        (ALICE.user_id, "join_request", f"Bob wants to pick up {sid[:8]}")
    ]
    assert log["notices"][0]["body"] == (
        "Allow lets everyone in the workspace pick up this session on their Mac. It uploads the whole "
        "conversation and your uncommitted changes as they are. Nothing is redacted."
    )
    [row] = log["activity"]
    assert row["subtype"] == "join_request" and row["request"] == "pickup"
    assert row["requesterId"] == BOB.user_id and row["requestId"] == first["requestId"]
    assert len(log["presence"]) == 1
    assert [e.action for e in (await service.list_audit(ALICE, sid)).entries] == ["pickup_requested"]


@pytest.mark.asyncio
async def test_own_session_codex_and_already_on_are_refused_or_ignored(transport):
    sid = await claude_session(transport)
    with recorded() as log:
        with pytest.raises(ApiProblem) as own:
            await service.request_pickup_access(ALICE, sid)
        assert own.value.error.code == "LOCAL_SESSION_FORBIDDEN"
        await pickup.set_pickup(ALICE, sid, True)
        revision = (await stored(sid))["revision"]
        public = await service.request_pickup_access(BOB, sid)
        assert public.pickup_request is None
        assert (await stored(sid))["revision"] == revision
        assert log["notices"] == [] and log["activity"] == []


@pytest.mark.asyncio
async def test_a_codex_session_cannot_be_asked_for(transport):
    sid = await start(transport)  # the relay fixture registers Codex
    with recorded(), pytest.raises(ApiProblem) as codex:
        await service.request_pickup_access(BOB, sid)
    assert codex.value.error.code == "LOCAL_SESSION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_allow_turns_pick_up_on_and_tells_the_requester(transport):
    sid = await claude_session(transport)
    with recorded() as log:
        await service.request_pickup_access(BOB, sid)
        rid = (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"]
        with pytest.raises(ApiProblem) as stale:
            await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id="stale")
        assert stale.value.error.code == "LOCAL_SESSION_CONFLICT"
        with pytest.raises(ApiProblem) as other:
            await service.decide_pickup_request(CAROL, sid, BOB.user_id, "allow", request_id=rid)
        assert other.value.error.code == "LOCAL_SESSION_FORBIDDEN"
        log["presence"].clear()
        calls = []
        real = pickup.set_pickup

        async def spy(auth, session_id, enabled, **kw):
            calls.append((auth.user_id, session_id, enabled))
            return await real(auth, session_id, enabled, **kw)

        with patch.object(pickup, "set_pickup", spy):
            public = await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    assert calls == [(ALICE.user_id, sid, True)]
    assert public.pickup.allowed is True and public.pending_pickups is None
    row = await stored(sid)
    assert row["pickupAllowed"] is True and BOB.user_id not in row["pickupRequests"]
    answer = log["notices"][-1]
    assert (answer["user"], answer["state"], answer["title"]) == (
        BOB.user_id,
        "join_allowed",
        f"Alice let you pick up {sid[:8]}",
    )
    # The owner's Mac lifts its freeze at once: presence goes out with the fresh row.
    [pushed] = log["presence"]
    assert pushed["pickupAllowed"] is True and service.pending_pickups(pushed) == {}
    assert "pickup_on" in [e.action for e in (await service.list_audit(ALICE, sid)).entries]


@pytest.mark.asyncio
async def test_decline_then_ask_again(transport):
    sid = await claude_session(transport)
    with recorded() as log:
        await service.request_pickup_access(BOB, sid)
        rid = (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"]
        public = await service.decide_pickup_request(ALICE, sid, BOB.user_id, "decline", request_id=rid)
        assert public.pending_pickups is None
        assert (await stored(sid))["pickupRequests"][BOB.user_id]["state"] == "declined"
        assert (await service.get_session(BOB, sid)).pickup_request == "declined"
        answer = log["notices"][-1]
        assert (answer["user"], answer["state"], answer["title"]) == (
            BOB.user_id,
            "join_declined",
            f"Alice declined your request to pick up {sid[:8]}",
        )
        again = await service.request_pickup_access(BOB, sid)
    assert again.pickup_request == "pending"
    assert (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"] != rid
    assert [e.action for e in (await service.list_audit(ALICE, sid)).entries] == [
        "pickup_requested",
        "pickup_request_declined",
        "pickup_requested",
    ]


@pytest.mark.asyncio
async def test_a_decline_that_lands_first_fences_allow(transport):
    """Allow read the request pending; a Decline from another surface committed before its write."""
    sid = await claude_session(transport)
    with recorded():
        await service.request_pickup_access(BOB, sid)
        rid = (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"]
        pending = await service.authorize_session(ALICE, None, sid)
        await service.decide_pickup_request(ALICE, sid, BOB.user_id, "decline", request_id=rid)
        real = service.authorize_session
        reads = []

        async def stale_first(*args, **kw):
            reads.append(1)
            return pending if len(reads) == 1 else await real(*args, **kw)

        with patch.object(service, "authorize_session", stale_first), pytest.raises(ApiProblem) as late:
            await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    assert late.value.error.code == "LOCAL_SESSION_CONFLICT" and len(reads) == 3  # decide, put_link, set_pickup
    assert "shareLink" not in await stored(sid)  # the link Allow made first was taken back
    row = await stored(sid)
    assert not row.get("pickupAllowed") and row["pickupRequests"][BOB.user_id]["state"] == "declined"


@pytest.mark.asyncio
async def test_allow_on_a_paused_session_keeps_the_request(transport):
    sid = await claude_session(transport)
    with recorded():
        await service.request_pickup_access(BOB, sid)
        rid = (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"]
        await service.set_sharing(ALICE, sid, False)
        with pytest.raises(ApiProblem, match="Resume sharing"):
            await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    assert (await stored(sid))["pickupRequests"][BOB.user_id]["state"] == "pending"


@pytest.mark.asyncio
async def test_the_owners_switch_clears_every_pending_request(transport):
    sid = await claude_session(transport)
    with recorded() as log:
        await service.request_pickup_access(BOB, sid)
        await service.request_pickup_access(CAROL, sid)
        log["presence"].clear()
        await pickup.set_pickup(ALICE, sid, True)
    row = await stored(sid)
    assert row["pickupRequests"] == {}
    assert service.pending_pickups(row) == {}
    assert len(log["presence"]) == 1  # the Mac lifts its freeze on the switch too


@pytest.mark.asyncio
async def test_who_sees_what(transport):
    sid = await claude_session(transport)
    with recorded():
        await service.request_pickup_access(BOB, sid)
    rid = (await stored(sid))["pickupRequests"][BOB.user_id]["requestId"]
    row = await stored(sid)
    bob = service._session_public(row, BOB.user_id).model_dump(by_alias=True)
    alice = service._session_public(row, ALICE.user_id).model_dump(by_alias=True)
    carol = service._session_public(row, CAROL.user_id).model_dump(by_alias=True)
    assert bob["pickupRequest"] == "pending" and "pendingPickups" not in bob
    assert alice["pendingPickups"] == [BOB.user_id] and alice["pendingPickupIds"] == {BOB.user_id: rid}
    assert "pickupRequest" not in alice
    assert not {"pickupRequest", "pendingPickups", "pendingPickupIds"} & set(carol)


@pytest.mark.asyncio
async def test_pickup_request_routes(transport):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from httpx import ASGITransport, AsyncClient

    from core.auth import get_current_user
    from routers import messaging

    sid = await claude_session(transport)
    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda r, exc: JSONResponse(status_code=exc.status_code, content=exc.body()))
    base = f"/api/v1/messaging/sessions/{sid}/pickup-requests"
    with recorded():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides[get_current_user] = lambda: BOB
            asked = await client.post(base)
            assert asked.status_code == 200 and asked.json()["pickupRequest"] == "pending"
            decide = f"{base}/{BOB.user_id}"
            assert (await client.post(decide, json={"decision": "allow"})).status_code == 403
            app.dependency_overrides[get_current_user] = lambda: ALICE
            assert (await client.post(decide, json={"decision": "maybe"})).status_code == 422
            allowed = await client.post(decide, json={"decision": "allow"})
            assert allowed.status_code == 200 and allowed.json()["pickup"]["allowed"] is True


@pytest.mark.asyncio
async def test_nothing_to_ask_or_allow_once_the_snapshot_expired(transport):
    sid = await claude_session(transport)
    with recorded():
        await service.request_pickup_access(BOB, sid)
        with patch.object(pickup, "_expired", lambda row, now=None: True):
            with pytest.raises(ApiProblem) as asked:
                await service.request_pickup_access(CAROL, sid)
            assert asked.value.error.code == "LOCAL_SESSION_NOT_FOUND"
            with pytest.raises(ApiProblem) as allowed:
                await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow")
            assert allowed.value.error.code == "LOCAL_SESSION_NOT_FOUND"
            row = await stored(sid)
            assert service._session_public(row, BOB.user_id).pickup_request is None
            assert not service._session_public(row, ALICE.user_id).model_dump().get("pending_pickups")
