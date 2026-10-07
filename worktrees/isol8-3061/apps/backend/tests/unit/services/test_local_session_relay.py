"""Transport integration: real Moto transactions/connections, stub only outbound HTTP."""

import asyncio
import importlib
import json
import time
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_sessions as registry_tests
from tests.unit.services.test_local_sessions import ALICE, BOB, CLI, MAC, OTHER_ROOM, OWNER, ROOM, command

db = registry_tests.db
# The desktop's Rust tests assert the same documents; Rust-versus-Python skew has cost a prod outage.
FIXTURES = Path(__file__).resolve().parents[5] / "packages" / "shared" / "fixtures" / "local-session-frames"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def transport(db):
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    connections = resource.create_table(
        TableName="relay-connections",
        KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table = db[0]
    table.update(
        AttributeDefinitions=[{"AttributeName": "memberKey", "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[
            {
                "Create": {
                    "IndexName": "by-member",
                    "KeySchema": [{"AttributeName": "memberKey", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            }
        ],
    )
    for room in (ROOM, OTHER_ROOM):
        table.update_item(
            Key={"pk": f"ROOM#{room}", "sk": "META"},
            UpdateExpression="SET deliveryKey = :key",
            ExpressionAttributeValues={":key": room},
        )
        table.update_item(
            Key={"pk": f"ROOM#{room}", "sk": f"MEMBER#human#{ALICE.user_id}"},
            UpdateExpression="SET memberKey = :member, roomId = :room, ownerId = :owner",
            ExpressionAttributeValues={
                ":member": messaging_repo.member_key(OWNER, "human", ALICE.user_id),
                ":room": room,
                ":owner": OWNER,
            },
        )
    sent, hints = [], []
    management = ManagementApiClient(endpoint_url="https://management.invalid", region_name="us-east-1")

    def post(**kwargs):
        sent.append((kwargs["ConnectionId"], json.loads(kwargs["Data"])))

    async def publish_http(_self, endpoint, body, headers):
        hints.append(json.loads(body))
        return __import__("httpx").Response(200, json={"successful": [{"identifier": "ok"}], "failed": []})

    with (
        patch.object(management._client, "post_to_connection", side_effect=post),
        patch("core.services.live_events.LiveEventPublisher._post", publish_http),
        patch.object(messaging_repo.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.invalid/event"),
    ):
        yield connections, ConnectionService(table_name=connections.name), management, sent, hints


def relay(transport):
    module = importlib.import_module("core.services.local_session_relay")
    return module.LocalSessionRelay(transport[1], transport[2])


def connect(transport, cid="conn-1", stamp="1000", **changes):
    row = dict(
        connectionId=cid,
        connectedAt=stamp,
        userId=ALICE.user_id,
        orgId=OWNER,
        ownerId=OWNER,
        connectionType="local-session",
        authKind="device",
        clientId="lightbulb-local-sessions",
        deviceId=MAC,
    )
    row.update(changes)
    transport[0].put_item(Item=row)


def registration(**changes):
    session = dict(
        provider="codex",
        profileFingerprint="f" * 64,
        nativeConversationId="thread-1",
        generation="generation-1",
        providerVersion="0.149.0",
        status="busy",
        activeTurnId="turn-1",
    )
    session.update(changes)
    return {"type": "local.session.register", "session": session}


def heartbeat(sid, **changes):
    """The golden device frame, defaulted to what `start()` registered: silent about every field.

    Labels are omit-means-unchanged on the wire, so the silent shape nulls them.
    """
    frame = fixture("device-heartbeat")
    defaults = {
        "sessionId": sid,
        "generation": "generation-1",
        "activeTurnId": "turn-1",
        "title": None,
        "cwdName": None,
    }
    frame["sessions"][0].update({**defaults, **changes})
    return frame


async def start(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    response = await relay(transport).handle_message("conn-1", registration())
    return response["sessionId"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "member", "expiry"])
async def test_agent_dispatch_uses_typed_author_and_fences_revocation_or_expiry(transport, db, monkeypatch, change):
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": "MEMBER#agent#friday", "kind": "agent", "id": "friday"})
    caller = grants.verify(grants.mint(OWNER, ROOM, "friday", OTHER_ROOM))
    sid = await start(transport)
    await service.submit_command(caller, ROOM, sid, command(service))
    transport[3].clear()
    real = messaging_repo._transact
    fired = False

    async def race(items):
        nonlocal fired
        if not fired and any(op.get("Put", {}).get("Item", {}).get("state") == "dispatched" for op in items):
            fired = True
            if change == "member":
                db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": "MEMBER#agent#friday"})
            elif change == "expiry":
                monkeypatch.setattr(time, "time", lambda: caller.expires_at)
        await real(items)

    with patch.object(messaging_repo, "_transact", race):
        await relay(transport).dispatch_session(OWNER, sid)
    assert fired
    if change == "none":
        wire = transport[3][0][1]["command"]
        assert wire["actor"] == {"kind": "agent", "id": "friday"}
        assert "actorUserId" not in wire and "invocationId" not in wire
        assert wire["invocationExpiresAt"] == caller.expires_at
    else:
        assert transport[3] == []
        assert (await repo.get_command(OWNER, sid, "request-1"))["state"] != "dispatched"


@pytest.mark.asyncio
async def test_dispatch_labels_the_sender_with_room_handle_and_name(transport, db):
    from core.services.messaging import with_human_handles

    db[0].put_item(
        Item={
            "pk": f"ROOM#{ROOM}",
            "sk": f"MEMBER#human#{BOB.user_id}",
            "kind": "human",
            "id": BOB.user_id,
            "ownerId": OWNER,
            "displayName": "Bob Example",
            "joinedSeq": 2,
            "memberKey": messaging_repo.member_key(OWNER, "human", BOB.user_id),
            "roomId": ROOM,
        }
    )
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    commands = [body for _, body in transport[3] if body["type"] == "local.session.command"]
    expected = next(m for m in with_human_handles(await messaging_repo.list_members(ROOM)) if m["id"] == BOB.user_id)
    assert commands[0]["actor"] == {"kind": "human", "handle": expected["mentionHandle"], "displayName": "Bob Example"}
    assert commands[0]["command"]["actorUserId"] == BOB.user_id


@pytest.mark.asyncio
async def test_optional_cli_revocation_does_not_revoke_native_mac_sharing(transport, db):
    sid = await start(transport)
    db[1].update_item(
        Key={"owner_id": OWNER}, UpdateExpression="REMOVE paired_devices.#cli", ExpressionAttributeNames={"#cli": CLI}
    )
    # A removed association cannot be newly enrolled, but existing Mac consent
    # and every ongoing local-session transaction are independent of that node.
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport, "conn-2", "2000")
    assert (await relay(transport).handle_message("conn-2", registration()))["sessionId"] == sid
    await relay(transport).handle_message(
        "conn-2",
        {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": "independent",
            "afterSeq": 0,
            "events": [{"kind": "text", "role": "assistant", "text": "still shared", "truncated": False}],
        },
    )
    assert (await service.list_events(BOB, ROOM, sid)).events[0].text == "still shared"
    resources = await service.list_resources(BOB, ROOM)
    assert resources.devices[0].cli_device_id is None
    await service.submit_command(BOB, ROOM, sid, command(service))
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "dispatched"
    await relay(transport).handle_message(
        "conn-2",
        {
            "type": "local.session.receipt",
            "sessionId": sid,
            "generation": "generation-1",
            "requestId": "request-1",
            "outcome": "applied",
            "providerMessageId": "native",
            "held": True,
        },
    )
    db[1].update_item(
        Key={"owner_id": OWNER}, UpdateExpression="REMOVE paired_devices.#mac", ExpressionAttributeNames={"#mac": MAC}
    )
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-2", registration())
    with pytest.raises(ApiProblem):
        await service.list_events(BOB, ROOM, sid)
    with pytest.raises(ApiProblem):
        await service.submit_command(BOB, ROOM, sid, command(service, requestId="after-mac-revocation"))


@pytest.mark.asyncio
async def test_native_capability_restriction_survives_reconnect_and_heartbeat(transport):
    sid = await start(transport)
    response = await relay(transport).handle_message(
        "conn-1", registration(status="unknown", controlUnavailableReason="endpoint-unavailable")
    )
    assert response["sessionId"] == sid
    assert abs(response["serverTime"] - time.time()) < 2
    public = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert public.status == "unknown"
    assert public.provider_version == "0.149.0"
    assert public.control_unavailable_reason == "endpoint-unavailable"
    assert public.capabilities.send_mode == "none"
    await relay(transport).handle_message("conn-1", registration(status="unknown"))
    public = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert public.capabilities.send_mode == "queue"
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="unknown", controlUnavailableReason="runtime-unverified")
    )
    public = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert public.capabilities.send_mode == "none"
    assert not public.capabilities_verified
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", registration(controlUnavailableReason="/private/path"))


@pytest.mark.asyncio
async def test_backend_local_admission_uses_canonical_mac_and_never_opens_gateway(transport):
    # Node hosts and Paperclip operators moved to routers/gateway_relay.py; this
    # router now has no admission path other than local-session, so the old
    # "must not open node/operator" probes are gone with the code they probed.
    from routers.websocket_chat import router

    app = FastAPI()
    app.include_router(router, prefix="/ws")
    app.add_exception_handler(
        ApiProblem, lambda _, exc: JSONResponse(status_code=exc.status_code, content={"error": exc.error.code})
    )
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    connect(transport)
    headers = {
        "x-connection-id": "conn-1",
        "x-user-id": ALICE.user_id,
        "x-org-id": OWNER,
        "x-auth-kind": "device",
        "x-verified-device-id": MAC,
        "x-client-id": "lightbulb-local-sessions",
    }
    with (
        patch.object(messaging_repo.settings, "WS_ENFORCE_INTERNAL_SECRET", False),
        patch("routers.websocket_chat.get_connection_service", return_value=transport[1]),
        patch("core.services.local_session_relay.get_local_session_relay", return_value=relay(transport)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/ws/connect", headers=headers)).status_code == 200
    for changes in (
        {"deviceId": CLI},
        {"deviceId": "c" * 64},
        {"authKind": "paperclip_service"},
        {"clientId": "node-host"},
        {"userId": BOB.user_id},
        {"orgId": "org_other"},
    ):
        connect(transport, **changes)
        with pytest.raises(ApiProblem):
            await relay(transport).connect("conn-1")


@pytest.mark.asyncio
async def test_ingestion_is_bounded_ordered_deduplicated_and_emits_only_room_hints(transport, db):
    sid = await start(transport)
    # Registration's own room + workspace hints are covered elsewhere; transcript
    # batches must emit room hints only.
    transport[4].clear()
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "batch-1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": "private transcript 😀", "truncated": False}],
    }
    first = await relay(transport).handle_message("conn-1", body)
    assert first["afterSeq"] == 1
    assert await relay(transport).handle_message("conn-1", body) == first
    assert [(e.seq, e.text) for e in (await service.list_events(BOB, ROOM, sid)).events] == [
        (1, "private transcript 😀")
    ]
    for bad in (
        {**body, "ownerId": "org_other"},
        {**body, "type": "shell.exec"},
        {**body, "events": [{"kind": "reasoning", "role": "assistant", "text": "secret"}]},
        {**body, "events": [{"kind": "text", "role": "assistant", "text": "x" * 16384}]},
        {**body, "generation": "old"},
        {**body, "batchId": "batch-new", "afterSeq": 0},
    ):
        with pytest.raises(ApiProblem):
            await relay(transport).handle_message("conn-1", bad)
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message(
            "conn-1", {**body, "events": [{"kind": "text", "role": "assistant", "text": "changed"}]}
        )
    assert transport[4]
    for hint in transport[4]:
        event = json.loads(hint["events"][0])
        assert "/rooms/" in hint["channel"]
        assert event["type"] == "room.resources.changed"
        assert event["payload"] == {"roomId": event["roomId"]}
        assert "private transcript" not in json.dumps(hint)
    # Removing owner membership removes this room from eligible hint channels.
    db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{ALICE.user_id}"})
    transport[4].clear()
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert all(ROOM not in hint["channel"] for hint in transport[4])


@pytest.mark.asyncio
async def test_registration_server_bounds_compatibility_liveness_and_reconnect(transport, db):
    sid = await start(transport)
    row = await repo.get_session(OWNER, sid)
    assert row["capabilities"] == {"sendMode": "queue", "steer": False, "interrupt": False}
    assert 0 < row["onlineUntil"] - int(time.time()) <= 30
    for changes in ({"onlineUntil": 9999999999}, {"capabilitiesVerified": True}, {"capabilities": {"interrupt": True}}):
        with pytest.raises(ApiProblem):
            await relay(transport).handle_message("conn-1", registration(**changes))
    connect(transport, "conn-2", "2000")
    assert (await relay(transport).handle_message("conn-2", registration()))["sessionId"] == sid
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", registration())
    # The superseded connection cannot renew the session the replacement now owns.
    ack = await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert ack["rejected"] == [sid]
    # Deleted older connection cannot affect the replacement.
    transport[0].delete_item(Key={"connectionId": "conn-1"})
    assert (await repo.get_session(OWNER, sid))["connectionId"] == "conn-2"
    await relay(transport).handle_message(
        "conn-2",
        {
            **registration(generation="generation-2", controlUnavailableReason="unsupported-version"),
            "previousGeneration": "generation-1",
        },
    )
    row = await repo.get_session(OWNER, sid)
    assert row["generation"] == "generation-2" and not row["capabilitiesVerified"]
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-2", registration())
    db[1].update_item(
        Key={"owner_id": OWNER}, UpdateExpression="REMOVE paired_devices.#mac", ExpressionAttributeNames={"#mac": MAC}
    )
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-2", heartbeat(sid, generation="generation-2", status="idle"))


@pytest.mark.asyncio
async def test_two_workers_deliver_in_command_order_and_receipts_are_durable(transport):
    sid = await start(transport)
    first = await service.submit_command(BOB, ROOM, sid, command(service))
    second = await service.submit_command(BOB, ROOM, sid, command(service, requestId="request-2"))
    transport[3].clear()
    # Fresh objects share only DynamoDB and the external Management API.
    await asyncio.gather(relay(transport).dispatch_session(OWNER, sid), relay(transport).dispatch_session(OWNER, sid))
    commands = [body for _, body in transport[3] if body["type"] == "local.session.command"]
    assert len(commands) == 1 and commands[0]["command"]["requestId"] == first.request_id
    assert commands[0]["command"]["actorUserId"] == BOB.user_id
    assert commands[0]["command"]["roomId"] == ROOM
    assert (await repo.get_command(OWNER, sid, first.request_id))["state"] == "dispatched"
    outcome = {
        "type": "local.session.receipt",
        "sessionId": sid,
        "generation": "generation-1",
        "requestId": first.request_id,
        "outcome": "applied",
        "providerMessageId": "native-1",
        "held": True,
    }
    await relay(transport).handle_message("conn-1", outcome)
    await relay(transport).handle_message("conn-1", outcome)
    receipt = await service.get_command(BOB, ROOM, sid, first.request_id)
    assert receipt.state == "applied" and receipt.held is True and receipt.provider_message_id == "native-1"
    commands = [body for _, body in transport[3] if body["type"] == "local.session.command"]
    assert [c["command"]["requestId"] for c in commands] == [first.request_id, second.request_id]
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", {**outcome, "outcome": "rejected"})


@pytest.mark.asyncio
@pytest.mark.parametrize("network_result", ["gone", "timeout", "delivered"])
async def test_uncertain_delivery_never_replays_on_reconnect(transport, network_result):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    calls = []

    def network(**kwargs):
        calls.append(kwargs)
        if network_result == "gone":
            raise ClientError({"Error": {"Code": "GoneException", "Message": ""}}, "PostToConnection")
        if network_result == "timeout":
            raise TimeoutError("ambiguous")

    with patch.object(transport[2]._client, "post_to_connection", side_effect=network):
        await relay(transport).dispatch_session(OWNER, sid)
        assert len(calls) == 1
        connect(transport, "conn-2", "2000")
        await relay(transport).handle_message("conn-2", registration())
        await relay(transport).dispatch_session(OWNER, sid)
        # registration responses are returned to router; only commands use direct push here.
        assert len(calls) == 1
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "unknown"
    await relay(transport).handle_message(
        "conn-2",
        {
            "type": "local.session.receipt",
            "sessionId": sid,
            "generation": "generation-1",
            "requestId": "request-1",
            "outcome": "applied",
            "providerMessageId": "native-1",
        },
    )
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "applied"


@pytest.mark.asyncio
async def test_pre_network_disconnect_is_retryable_but_expired_pending_is_rejected(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    transport[0].delete_item(Key={"connectionId": "conn-1"})
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "pending"
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", registration())
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "dispatched"
    await service.submit_command(BOB, ROOM, sid, command(service, requestId="expired"))
    with patch("time.time", return_value=time.time() + 31):
        await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "expired"))["state"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["member", "enrollment", "pairing", "connection"])
async def test_pre_dispatch_transaction_rechecks_authorization_after_claim(transport, db, mutation):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    real_transact = messaging_repo._transact
    fired = False

    async def race(items):
        nonlocal fired
        # Inject revocation immediately before the durable dispatch boundary.
        if not fired and any(op.get("Put", {}).get("Item", {}).get("state") == "dispatched" for op in items):
            fired = True
            if mutation == "member":
                db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{BOB.user_id}"})
            elif mutation == "enrollment":
                await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=False))
            elif mutation == "pairing":
                db[1].update_item(
                    Key={"owner_id": OWNER},
                    UpdateExpression="REMOVE paired_devices.#mac",
                    ExpressionAttributeNames={"#mac": MAC},
                )
            else:
                transport[0].delete_item(Key={"connectionId": "conn-1"})
        await real_transact(items)

    transport[3].clear()
    with patch.object(messaging_repo, "_transact", race):
        await relay(transport).dispatch_session(OWNER, sid)
    assert fired and transport[3] == []
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] != "dispatched"


@pytest.mark.asyncio
async def test_full_management_encoding_is_bounded_before_dispatch(transport):
    sid = await start(transport)
    # Valid public UTF-8 request expands to >16 KiB under Management API ensure_ascii=True.
    await service.submit_command(BOB, ROOM, sid, command(service, text="😀" * 1900))
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    assert transport[3] == []
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "rejected"


@pytest.mark.asyncio
async def test_text_rate_is_shared_and_lifecycle_flush_is_not_delayed(transport):
    sid = await start(transport)
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "batch-1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": "one"}],
    }
    with patch("time.time", return_value=2000000000):
        await relay(transport).handle_message("conn-1", body)
        with pytest.raises(ApiProblem) as exc:
            await relay(transport).handle_message("conn-1", {**body, "batchId": "batch-2", "afterSeq": 1})
        assert exc.value.error.code == "RATE_LIMITED"
        result = await relay(transport).handle_message(
            "conn-1",
            {
                **body,
                "batchId": "boundary",
                "afterSeq": 1,
                "events": [{"kind": "lifecycle", "role": "system", "text": "idle"}],
            },
        )
        assert result["afterSeq"] == 2


@pytest.mark.asyncio
async def test_event_ingestion_publishes_a_hint_at_most_once_per_second(transport):
    sid = await start(transport)
    transport[4].clear()
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "b1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": "one"}],
    }
    lifecycle = {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "turn_end"}
    with patch("time.time", return_value=2000000000.0):
        await relay(transport).handle_message("conn-1", body)
        first = [h for h in transport[4] if ROOM in h["channel"]]
        assert len(first) == 1
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b2", "afterSeq": 1, "events": [lifecycle]})
        assert len([h for h in transport[4] if ROOM in h["channel"]]) == 1
    with patch("time.time", return_value=2000000002.0):
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b3", "afterSeq": 2, "events": [lifecycle]})
    assert len([h for h in transport[4] if ROOM in h["channel"]]) == 2


@pytest.mark.asyncio
async def test_router_dispatches_post_but_receipt_get_is_read_only(transport):
    from core.auth import get_current_user
    from routers.messaging import router

    sid = await start(transport)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: BOB
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=relay(transport)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = f"/messaging/rooms/{ROOM}/local-sessions/{sid}/commands"
            result = await client.post(path, json=command(service).model_dump(by_alias=True))
            assert result.status_code == 202
            assert result.json()["state"] == "dispatched"
            before = await repo.list_commands(OWNER, sid)
            transport[3].clear()
            with patch("time.time", return_value=time.time() + 60):
                result = await client.get(path + "/request-1")
            assert result.status_code == 200 and transport[3] == []
            assert await repo.list_commands(OWNER, sid) == before


@pytest.mark.asyncio
async def test_late_network_failure_cannot_regress_native_receipt(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))

    async def network(_cid, _body):
        await relay(transport).handle_message(
            "conn-1",
            {
                "type": "local.session.receipt",
                "sessionId": sid,
                "generation": "generation-1",
                "requestId": "request-1",
                "outcome": "applied",
                "providerMessageId": "native-1",
            },
        )
        raise TimeoutError("late network result")

    with patch.object(transport[2], "send_message_async", side_effect=network):
        await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "applied"


@pytest.mark.asyncio
async def test_receipt_can_reconcile_old_generation_journal_on_new_connection(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    await relay(transport).dispatch_session(OWNER, sid)
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    await relay(transport).handle_message(
        "conn-2",
        {
            "type": "local.session.receipt",
            "sessionId": sid,
            "generation": "generation-1",
            "requestId": "request-1",
            "outcome": "applied",
            "providerMessageId": "native-1",
        },
    )
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "applied"


@pytest.mark.asyncio
async def test_expired_dispatch_claim_never_posts_after_deadline(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    real_transact, now = messaging_repo._transact, time.time()

    async def delayed_commit(items):
        await real_transact(items)
        if any(op.get("Put", {}).get("Item", {}).get("state") == "dispatched" for op in items):
            clock.return_value = now + 31

    transport[3].clear()
    with patch("time.time", return_value=now) as clock, patch.object(messaging_repo, "_transact", delayed_commit):
        await relay(transport).dispatch_session(OWNER, sid)
    assert transport[3] == []
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "rejected"


@pytest.mark.asyncio
async def test_crashed_pre_dispatch_claim_can_be_reclaimed_after_five_seconds(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    real_transact, now = messaging_repo._transact, time.time()

    async def crash_after_claim(items):
        await real_transact(items)
        if any(
            op.get("Put", {}).get("Item", {}).get("state") == "pending" and op["Put"]["Item"].get("claimUntil")
            for op in items
        ):
            raise RuntimeError("worker died before dispatch")

    transport[3].clear()
    with patch.object(messaging_repo, "_transact", crash_after_claim):
        with pytest.raises(RuntimeError):
            await relay(transport).dispatch_session(OWNER, sid)
    await relay(transport).dispatch_session(OWNER, sid)
    assert transport[3] == []
    with patch("time.time", return_value=now + 6):
        await relay(transport).dispatch_session(OWNER, sid)
    assert len(transport[3]) == 1
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "dispatched"


@pytest.mark.asyncio
async def test_reconnect_exposes_only_blocking_request_for_journal_reconciliation(transport):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    await relay(transport).dispatch_session(OWNER, sid)
    connect(transport, "conn-2", "2000")
    response = await relay(transport).handle_message("conn-2", registration())
    assert response["reconcile"] == {"requestId": "request-1", "generation": "generation-1", "state": "unknown"}
    assert "continue" not in json.dumps(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_outcome", ["unknown", "dispatched"])
@pytest.mark.parametrize("current_outcome", ["unknown", "dispatched"])
async def test_native_successor_unblocks_new_input_without_replaying_old_unknown(
    transport, old_outcome, current_outcome
):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    with patch.object(transport[2], "send_message_async", return_value=old_outcome == "dispatched"):
        await relay(transport).dispatch_session(OWNER, sid)
    await service.submit_command(BOB, ROOM, sid, command(service, requestId="same-generation"))
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "same-generation"))["state"] == "pending"
    # Same conversation, verified process successor; no UI-generated identity.
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "unknown"
    request = command(service, requestId="new-generation", expectedGeneration="generation-2")
    await service.submit_command(BOB, ROOM, sid, request)
    transport[3].clear()
    with patch.object(transport[2], "send_message_async", return_value=current_outcome == "dispatched") as post:
        response = await relay(transport).handle_message("conn-1", heartbeat(sid, generation="generation-2"))
    assert post.await_count == 1
    assert post.call_args.args[1]["command"]["requestId"] == "new-generation"
    assert response["reconcile"] == [
        {
            "sessionId": sid,
            "requestId": "new-generation",
            "generation": "generation-2",
            "state": current_outcome,
        }
    ]
    assert (await repo.get_command(OWNER, sid, "same-generation"))["state"] == "rejected"
    assert (await service.submit_command(BOB, ROOM, sid, command(service))).state == "unknown"
    with pytest.raises(ApiProblem):
        await service.submit_command(BOB, ROOM, sid, command(service, requestId="stale-generation"))
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "unknown"
    await relay(transport).handle_message(
        "conn-1",
        {
            "type": "local.session.receipt",
            "sessionId": sid,
            "generation": "generation-2",
            "requestId": "new-generation",
            "outcome": "applied",
            "providerMessageId": "native-2",
        },
    )
    response = await relay(transport).handle_message("conn-1", heartbeat(sid, generation="generation-2"))
    assert response["reconcile"] == [
        {"sessionId": sid, "requestId": "request-1", "generation": "generation-1", "state": "unknown"}
    ]


@pytest.mark.asyncio
async def test_unchanged_device_heartbeat_keeps_last_event_at_and_sends_no_hint(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
        await service.submit_command(BOB, ROOM, sid, command(service))
    before = await repo.get_session(OWNER, sid)
    transport[4].clear()
    with patch("time.time", return_value=10010):
        response = await relay(transport).handle_message("conn-1", heartbeat(sid))
    after = await repo.get_session(OWNER, sid)
    assert after["onlineUntil"] > before["onlineUntil"]
    assert after["revision"] != before["revision"]
    # The lease and delivery advance; activity and the room's refetch do not.
    assert after["lastEventAt"] == 10000
    assert response["type"] == "local.device.heartbeat.ack" and response["serverTime"] == 10010
    assert response["reconcile"] == [
        {"sessionId": sid, "requestId": "request-1", "generation": "generation-1", "state": "dispatched"}
    ]
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "dispatched"
    assert transport[4] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["status", "active-turn", "control", "workspace", "expired"])
async def test_visible_heartbeat_change_or_recovery_notifies_resources(transport, change):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    row = await repo.get_session(OWNER, sid)
    changes = {
        "status": {"status": "idle"},
        "active-turn": {"activeTurnId": "turn-2"},
        "control": {"controlUnavailableReason": "endpoint-unavailable"},
        "workspace": {"workspace": {"revision": 1, "coverage": "bounded", "changed": True, "overlap": False}},
    }
    transport[4].clear()
    with patch("time.time", return_value=row["onlineUntil"] + 1 if change == "expired" else 10010):
        response = await relay(transport).handle_message("conn-1", heartbeat(sid, **changes.get(change, {})))
    assert response["type"] == "local.device.heartbeat.ack"
    assert transport[4]
    # A workspace scan is worth a refetch, but it is not the user doing something.
    after = await repo.get_session(OWNER, sid)
    assert after["lastEventAt"] == (10000 if change in {"workspace", "expired"} else 10010)


@pytest.mark.asyncio
async def test_session_title_registers_updates_on_heartbeat_and_survives_silent_heartbeats(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    response = await relay(transport).handle_message(
        "conn-1", registration(title="Desktop and mobile notifications", cwdName="lightbulb")
    )
    sid = response["sessionId"]
    listed = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert (listed.title, listed.cwd_name) == ("Desktop and mobile notifications", "lightbulb")
    transport[4].clear()
    # activeTurnId matches the registered value: this heartbeat is silent about
    # everything except title/cwdName, which is the one omission under test.
    await relay(transport).handle_message("conn-1", heartbeat(sid))
    assert (await repo.get_session(OWNER, sid))["title"] == "Desktop and mobile notifications"
    assert transport[4] == []
    await relay(transport).handle_message("conn-1", heartbeat(sid, title="Renamed by the harness"))
    assert (await repo.get_session(OWNER, sid))["title"] == "Renamed by the harness"
    assert any(ROOM in hint["channel"] for hint in transport[4])


@pytest.mark.asyncio
async def test_background_job_registration_persists_its_kind_and_omitted_reads_interactive(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    job = await relay(transport).handle_message("conn-1", registration(kind="background"))
    assert (await repo.get_session(OWNER, job["sessionId"]))["kind"] == "background"
    # A desktop that predates background jobs sends no kind at all.
    terminal = await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2"))
    assert (await repo.get_session(OWNER, terminal["sessionId"]))["kind"] == "interactive"
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-3", kind="cron"))


@pytest.mark.asyncio
async def test_http_dispatch_failure_returns_durable_receipt_without_logging_text(transport, caplog):
    from core.auth import get_current_user
    from routers.messaging import router

    sid = await start(transport)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: BOB
    with patch("core.services.local_session_relay.get_local_session_relay", side_effect=RuntimeError("secret-token")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post(
                f"/messaging/rooms/{ROOM}/local-sessions/{sid}/commands",
                json=command(service).model_dump(by_alias=True),
            )
    assert result.status_code == 202 and result.json()["state"] == "pending"
    assert "secret-token" not in caplog.text and "continue" not in caplog.text


@pytest.mark.asyncio
async def test_repaired_device_cannot_reassign_existing_humans_session(transport, db):
    sid = await start(transport)
    db[1].update_item(
        Key={"owner_id": OWNER},
        UpdateExpression="SET paired_devices.#mac.userId = :bob",
        ExpressionAttributeNames={"#mac": MAC},
        ExpressionAttributeValues={":bob": BOB.user_id},
    )
    await service.set_enrollment(BOB, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    connect(transport, "conn-bob", "3000", userId=BOB.user_id)
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-bob", registration())
    assert (await repo.get_session(OWNER, sid))["humanUserId"] == ALICE.user_id


@pytest.mark.asyncio
async def test_deleted_connection_liveness_expires_without_touching_native_history(transport):
    sid = await start(transport)
    transport[0].delete_item(Key={"connectionId": "conn-1"})
    with patch("time.time", return_value=time.time() + 31):
        resources = await service.list_resources(BOB, ROOM)
    assert resources.sessions[0].status == "offline"
    assert await repo.get_session(OWNER, sid) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before-claim", "after-claim"])
async def test_session_revision_contention_retries_without_rejecting_authorized_input(transport, phase):
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    await service.submit_command(BOB, ROOM, sid, command(service, requestId="request-2"))
    authorize, calls = service.authorize_session, 0

    async def ingest_before_authorization(*args):
        nonlocal calls
        calls += 1
        if calls == (1 if phase == "before-claim" else 2):
            await relay(transport).handle_message(
                "conn-1",
                {
                    "type": "local.session.events",
                    "sessionId": sid,
                    "generation": "generation-1",
                    "batchId": "concurrent-lifecycle",
                    "afterSeq": 0,
                    "events": [{"kind": "lifecycle", "role": "system", "text": "busy"}],
                },
            )
        return await authorize(*args)

    transport[3].clear()
    with patch.object(service, "authorize_session", side_effect=ingest_before_authorization):
        await relay(transport).dispatch_session(OWNER, sid)
    first = await repo.get_command(OWNER, sid, "request-1")
    second = await repo.get_command(OWNER, sid, "request-2")
    assert first["state"] == "dispatched"
    assert second["state"] == "pending"
    assert [body["command"]["requestId"] for _, body in transport[3]] == ["request-1"]
    assert (await repo.get_session(OWNER, sid))["eventSeq"] == 1


@pytest.mark.asyncio
async def test_rich_events_round_trip_and_old_shape_still_accepted(transport):
    sid = await start(transport)
    call = {
        "kind": "tool",
        "role": "assistant",
        "text": "git status",
        "truncated": False,
        "ts": "2026-09-12T10:00:00.000Z",
        "itemId": "toolu_01",
        "tool": {"name": "Bash", "status": "running", "input": {"command": "git status"}},
    }
    result = {
        "kind": "tool",
        "role": "tool",
        "text": "On branch main",
        "truncated": False,
        "ts": "2026-09-12T10:00:01.000Z",
        "itemId": "uuid-2",
        "parentId": "toolu_01",
        "tool": {"name": "result", "status": "done", "result": {"stdout": "On branch main\n"}, "lines": 1},
    }
    prompt = {"kind": "text", "role": "user", "text": "hi", "actor": {"userId": BOB.user_id}}
    old = {"kind": "text", "role": "assistant", "text": "plain"}
    agent_prompt = {"kind": "text", "role": "user", "text": "from the agent", "actor": {"agentId": "agent_1"}}
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "b1",
        "afterSeq": 0,
        "events": [
            prompt,
            call,
            result,
            old,
            {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "turn_end"},
            agent_prompt,
        ],
    }
    ack = await relay(transport).handle_message("conn-1", body)
    assert ack["afterSeq"] == 6
    page = await service.list_events(BOB, ROOM, sid)
    events = page.model_dump(by_alias=True, exclude_none=True)["events"]
    assert events[0]["actor"] == {"userId": BOB.user_id}
    assert events[1]["tool"]["name"] == "Bash" and events[1]["ts"] == "2026-09-12T10:00:00.000Z"
    assert events[2]["parentId"] == "toolu_01" and events[2]["tool"]["lines"] == 1
    assert "tool" not in events[3] and "ts" not in events[3]
    assert events[4]["lifecycle"] == "turn_end"
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message(
            "conn-1",
            {
                **body,
                "batchId": "b2",
                "afterSeq": 6,
                "events": [{"kind": "text", "role": "user", "text": "x", "actor": {"userId": "u", "agentId": "a"}}],
            },
        )
    # The HTTP boundary is what the desktop and web renderer actually see: old-shape
    # events must not gain the six new fields as explicit nulls, and the legitimately
    # nullable page cursor must still come through as null, not absent.
    from core.auth import get_current_user
    from routers.messaging import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: BOB
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/messaging/rooms/{ROOM}/local-sessions/{sid}/events")
    assert response.status_code == 200
    wire = response.json()
    assert "nextAfterSeq" in wire and wire["nextAfterSeq"] is None
    wire_events = wire["events"]
    assert not set(wire_events[3]) & {"ts", "itemId", "parentId", "actor", "tool", "lifecycle"}
    assert wire_events[1]["tool"]["name"] == "Bash" and wire_events[1]["ts"] == "2026-09-12T10:00:00.000Z"
    # Nested optionals must be absent too: a null userId makes the web read an agent
    # follow-up as the Mac owner, and a null tool.lines makes a call's label render
    # as its own output.
    assert wire_events[5]["actor"] == {"agentId": "agent_1"}
    assert set(wire_events[1]["tool"]) == {"name", "status", "input"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "version", "reason", "verified"),
    [
        ("claude", "2.1.269", None, True),
        ("claude", "2.1.300", None, True),
        ("codex", "0.160.0", None, True),
        ("claude", "2.1.269", "unsupported-version", False),
        ("codex", "0.149.0", "runtime-unverified", False),
    ],
)
async def test_relay_capabilities_follow_the_desktop_verification_not_a_version_list(
    transport, provider, version, reason, verified
):
    # The desktop verifies the live runtime and fails closed with a controlUnavailableReason.
    # A backend version list can only drift from it (it did: the desktop learned Claude 2.1.269
    # on 2026-09-11 and the relay kept 2.1.269 sessions send-disabled).
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    response = await relay(transport).handle_message(
        "conn-1", registration(provider=provider, providerVersion=version, controlUnavailableReason=reason)
    )
    row = await repo.get_session(OWNER, response["sessionId"])
    assert row["capabilitiesVerified"] is verified
    assert row["capabilities"]["sendMode"] == ("queue" if verified else "none")


@pytest.mark.asyncio
async def test_cursor_conflict_is_logged_with_reason_and_seqs_but_never_text(transport, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="core.services.local_session_relay")
    sid = await start(transport)
    registered = next(r for r in caplog.records if r.getMessage() == "local session registered")
    assert registered.levelno == logging.INFO and registered.sid == sid and registered.cursor == 0
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "batch-1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": "private transcript", "truncated": False}],
    }
    await relay(transport).handle_message("conn-1", body)
    # afterSeq 5 has no stored row, so this is a cursor mismatch (row cursor 1), not a batch-hash replay.
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", {**body, "batchId": "batch-new", "afterSeq": 5})
    conflict = next(r for r in caplog.records if "conflict" in r.getMessage())
    assert conflict.levelno == logging.WARNING
    assert conflict.reason == "cursor" and conflict.expected == 1 and conflict.got == 5 and conflict.sid == sid
    assert "private transcript" not in caplog.text


@pytest.mark.asyncio
async def test_device_heartbeat_renews_listed_sessions_and_rejects_foreign_or_ended(transport, db):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    before = await repo.get_session(OWNER, sid)
    frame = heartbeat(sid, status="idle")
    frame["sessions"].append({**frame["sessions"][0], "sessionId": "not-mine", "generation": "x"})
    with patch("time.time", return_value=10010):
        response = await relay(transport).handle_message("conn-1", frame)
    after = await repo.get_session(OWNER, sid)
    assert after["onlineUntil"] == 10010 + 30 > before["onlineUntil"]
    assert after["status"] == "idle" and after["lastEventAt"] == 10010
    assert response["type"] == "local.device.heartbeat.ack"
    assert response["rejected"] == ["not-mine"]
    assert response["serverTime"] == 10010
    assert set(response) == set(fixture("device-heartbeat-ack"))


@pytest.mark.asyncio
async def test_ended_frame_marks_row_ended_idempotently_and_hints(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    transport[4].clear()
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with patch("time.time", return_value=10006):
        first = await relay(transport).handle_message("conn-1", frame)
        hinted = len(transport[4])
        second = await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["endedAt"] == 10005 and row["status"] == "ended" and row["lastEventAt"] == 10005
    assert first == second == {**fixture("session-ended-ack"), "sessionId": sid}
    assert hinted and len(transport[4]) == hinted
    assert (await service.list_resources(BOB, ROOM, include_ended=True)).sessions[0].status == "ended"
    # A device heartbeat that still lists the ended session gets it back as rejected,
    # and must not renew the lease of a session the desktop has stopped owning.
    with patch("time.time", return_value=10007):
        ack = await relay(transport).handle_message("conn-1", heartbeat(sid))
    assert ack["rejected"] == [sid]
    assert (await repo.get_session(OWNER, sid))["onlineUntil"] == row["onlineUntil"]
    # A resumed conversation re-registers under the same id and un-ends.
    with patch("time.time", return_value=10008):
        await relay(transport).handle_message("conn-1", registration())
    resumed = await repo.get_session(OWNER, sid)
    assert "endedAt" not in resumed and "endedReason" not in resumed
    assert resumed["status"] == "busy" and resumed["lastEventAt"] == 10008
    # A device clock ahead of the server cannot park a session in the future.
    with patch("time.time", return_value=10009):
        await relay(transport).handle_message("conn-1", {**frame, "endedAt": 99999999})
    clamped = await repo.get_session(OWNER, sid)
    assert clamped["endedAt"] == clamped["lastEventAt"] == 10009


@pytest.mark.asyncio
async def test_device_heartbeat_caps_one_frame_and_consecutive_frames_both_renew(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
        other = await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2"))
    frame = heartbeat(sid)
    frame["sessions"] = [{**frame["sessions"][0], "sessionId": f"session-{n}"} for n in range(25)]
    # The cap must reject the extra entries, not the whole envelope: 25 entries are ~5 KB.
    assert len(json.dumps(frame).encode()) < 16 * 1024
    with pytest.raises(ApiProblem) as excinfo:
        await relay(transport).handle_message("conn-1", frame)
    assert excinfo.value.error.code == "INVALID_REQUEST"
    # A device with more mirrors than one frame carries splits them; each frame stands alone.
    with patch("time.time", return_value=10010):
        await relay(transport).handle_message("conn-1", heartbeat(sid))
        await relay(transport).handle_message("conn-1", heartbeat(other["sessionId"]))
    assert (await repo.get_session(OWNER, sid))["onlineUntil"] == 10040
    assert (await repo.get_session(OWNER, other["sessionId"]))["onlineUntil"] == 10040


@pytest.mark.asyncio
async def test_legacy_per_session_heartbeat_is_rejected(transport):
    sid = await start(transport)
    with pytest.raises(ApiProblem) as excinfo:
        await relay(transport).handle_message(
            "conn-1",
            {"type": "local.session.heartbeat", "sessionId": sid, "generation": "generation-1", "status": "busy"},
        )
    assert excinfo.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_device_input_cannot_claim_a_server_owned_status(transport):
    # offline is the backend's verdict on a lapsed lease and ended is written by the
    # ended frame; a device claiming either would forge a state it does not own.
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    for frame in (heartbeat(sid, status="ended"), heartbeat(sid, status="offline"), registration(status="offline")):
        with pytest.raises(ApiProblem) as excinfo:
            await relay(transport).handle_message("conn-1", frame)
        assert excinfo.value.error.code == "INVALID_REQUEST"
    assert (await repo.get_session(OWNER, sid))["status"] == "busy"
    # The service-side registration model still defaults to offline: it is not device input.
    assert (
        service.SessionRegistration(
            provider="codex",
            profileFingerprint="f" * 64,
            nativeConversationId="t",
            generation="g",
            providerVersion="1.0",
        ).status
        == "offline"
    )


@pytest.mark.asyncio
async def test_register_and_events_stamp_last_event_at(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10000
    with patch("time.time", return_value=10020):
        await relay(transport).handle_message(
            "conn-1",
            {
                "type": "local.session.events",
                "sessionId": sid,
                "generation": "generation-1",
                "batchId": "batch-1",
                "afterSeq": 0,
                "events": [{"kind": "text", "role": "assistant", "text": "done", "truncated": False}],
            },
        )
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10020


@pytest.mark.asyncio
async def test_register_publishes_a_workspace_hint_to_every_user_stream(transport, db):
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    connect(transport)
    transport[4].clear()
    await relay(transport).handle_message("conn-1", registration())
    user_hints = [json.loads(h["events"][0]) for h in transport[4] if "/users/" in h["channel"]]
    assert len(user_hints) == 2
    assert {e["type"] for e in user_hints} == {"workspace.sessions.changed"}
    assert all(e["payload"] == {"humanUserId": ALICE.user_id} for e in user_hints)
    assert any("/rooms/" in h["channel"] for h in transport[4])


@pytest.mark.asyncio
async def test_register_workspace_hint_survives_a_failing_room_publish(transport, db):
    import core.services.live_events as live_events

    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    connect(transport)
    transport[4].clear()
    with (
        # A fresh publisher: the module singleton's signing lock binds to the
        # event loop of whichever test acquires it first, and pytest-asyncio
        # gives every test its own loop -- reusing it here is a test-isolation
        # hazard unrelated to the fix under test.
        patch.object(live_events, "_publisher", None),
        patch("core.services.live_events.LiveEventPublisher.publish_room", side_effect=RuntimeError("boom")),
    ):
        await relay(transport).handle_message("conn-1", registration())
    user_hints = [json.loads(h["events"][0]) for h in transport[4] if "/users/" in h["channel"]]
    assert len(user_hints) == 2
    assert {e["type"] for e in user_hints} == {"workspace.sessions.changed"}
    assert not any("/rooms/" in h["channel"] for h in transport[4])


@pytest.mark.asyncio
async def test_event_batches_hint_the_room_but_not_every_user_stream(transport, db):
    """A streaming teammate must not make every member refetch the workspace list
    once a second; liveness changes (register, heartbeat, ended) still do."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    sid = await start(transport)
    assert any("/users/" in h["channel"] for h in transport[4])
    transport[4].clear()
    await relay(transport).handle_message(
        "conn-1",
        {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": "b1",
            "afterSeq": 0,
            "events": [{"kind": "text", "role": "assistant", "text": "one"}],
        },
    )
    assert any("/rooms/" in h["channel"] for h in transport[4])
    assert not any("/users/" in h["channel"] for h in transport[4])
