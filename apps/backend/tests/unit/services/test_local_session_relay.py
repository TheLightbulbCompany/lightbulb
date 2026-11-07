"""Transport integration: real Moto transactions/connections, stub only outbound HTTP."""

import asyncio
import importlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pydantic
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


@pytest.fixture(autouse=True)
def _own_spawned_tasks():
    """``_spawn`` keeps tasks in a module-level set. A task an earlier test left pending on its
    own (now closed) loop never fires its discard callback, and ``gather(*_tasks)`` here then
    raises "attached to a different loop". Each test gathers only what it spawned."""
    from core.services import local_session_relay as module

    module._tasks.clear()
    yield
    module._tasks.clear()


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
            UpdateExpression="SET deliveryKey = :key, lastSeq = :zero, topCount = :zero, revision = :one",
            ExpressionAttributeValues={":key": room, ":zero": 0, ":one": 1},
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

    async def publish_http(_self, endpoint, body, headers, tag=None):
        hints.append(json.loads(body))
        return __import__("httpx").Response(200, json={"successful": [{"identifier": "ok"}], "failed": []})

    with (
        patch.object(management._client, "post_to_connection", side_effect=post),
        patch("core.services.live_events.LiveEventPublisher._post", publish_http),
        # A fresh publisher per test: the singleton's signing lock binds to the first test's
        # event loop, and every hint now fans out concurrently over the user streams.
        patch("core.services.live_events._publisher", None),
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
        holderId="ab12cd34",
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
async def test_start_id_arrives_late_belongs_only_to_owner_and_one_generation(transport):
    sid = await start(transport)
    start_id = "11111111-1111-4111-8111-111111111111"
    await relay(transport).handle_message("conn-1", registration(startId=start_id))
    row = await repo.get_session(OWNER, sid)
    assert row["startId"] == start_id
    assert service._session_public(row, ALICE.user_id).start_id == start_id
    assert service._session_public(row, BOB.user_id).start_id is None
    await relay(transport).handle_message("conn-1", registration())
    assert (await repo.get_session(OWNER, sid))["startId"] == start_id
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", registration(startId="22222222-2222-4222-8222-222222222222"))
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id).start_id is None


@pytest.mark.asyncio
async def test_start_id_of_a_run_that_ended_offline_arrives_with_its_end(transport):
    """The draining register of an ended run is unverified, so it may not carry a start identity.
    Its end frame does; a live unverified register is still refused."""
    start_id = "11111111-1111-4111-8111-111111111111"
    drain = dict(status="idle", activeTurnId=None, controlUnavailableReason="runtime-unverified")
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    with pytest.raises(ApiProblem) as refused:
        await relay(transport).handle_message("conn-1", registration(startId=start_id, **drain))
    assert refused.value.error.code == "LOCAL_SESSION_CONFLICT"
    sid = (await relay(transport).handle_message("conn-1", registration(**drain)))["sessionId"]
    assert "startId" not in await repo.get_session(OWNER, sid)
    ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 1}
    await relay(transport).handle_message("conn-1", {**ended, "startId": start_id})
    row = await repo.get_session(OWNER, sid)
    assert row["status"] == "ended" and row["startId"] == start_id
    assert service._session_public(row, ALICE.user_id).start_id == start_id
    assert service._session_public(row, BOB.user_id).start_id is None


@pytest.mark.asyncio
async def test_an_end_never_replaces_the_start_id_its_run_registered(transport):
    start_id = "11111111-1111-4111-8111-111111111111"
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", registration(startId=start_id))
    ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 1}
    await relay(transport).handle_message("conn-1", {**ended, "startId": "22222222-2222-4222-8222-222222222222"})
    row = await repo.get_session(OWNER, sid)
    assert row["status"] == "ended" and row["startId"] == start_id


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
async def test_an_online_dispatch_meters_its_latency_and_a_queued_send_does_not(transport):
    import core.services.local_session_relay as relay_module

    sid = await start(transport)
    await service.submit_command(ALICE, None, sid, command(service))
    transport[3].clear()
    with patch.object(relay_module, "put_metric") as metric:
        await relay(transport).dispatch_session(OWNER, sid)
    sent = [body["command"] for _, body in transport[3] if body["type"] == "local.session.command"]
    assert len(sent) == 1 and "createdAtMs" not in sent[0], "the stamp is server-side only"
    (latency,) = metric.call_args_list
    assert latency.args[0] == "session.command.dispatch.latency" and latency.args[2] == "Milliseconds"
    assert 0 <= latency.args[1] < 60_000

    # A follow-up queued while the Mac was offline waits on purpose: not a dispatch latency.
    applied = {"Key": {"pk": repo.session_pk(OWNER, sid), "sk": f"COMMAND#{sent[0]['requestId']}"}}
    messaging_repo._table().update_item(
        **applied,
        UpdateExpression="SET #s = :a",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":a": "applied"},
    )
    offline = {"Key": {"pk": repo.session_pk(OWNER, sid), "sk": "META"}, "UpdateExpression": "SET onlineUntil = :past"}
    messaging_repo._table().update_item(**offline, ExpressionAttributeValues={":past": int(time.time()) - 1})
    await service.submit_command(ALICE, None, sid, command(service, requestId="queued", text="later"))
    connect(transport, "conn-2", "2000")
    transport[3].clear()
    with patch.object(relay_module, "put_metric") as metric:
        await relay(transport).handle_message("conn-2", registration(status="idle"))
    assert [b["command"]["requestId"] for _, b in transport[3] if b["type"] == "local.session.command"] == ["queued"]
    assert "session.command.dispatch.latency" not in [c.args[0] for c in metric.call_args_list]


@pytest.mark.asyncio
async def test_dispatch_names_a_person_who_sent_from_no_room(transport, monkeypatch):
    """Prod 2026-10-05: a follow-up sent from the session's own page (roomId "") reached the
    owner's terminal as "A workspace member". It carries the name presence shows."""
    from unittest.mock import AsyncMock

    from core.services import local_session_relay as relay_module, session_presence

    names = AsyncMock(return_value="Isol8 Admin")
    monkeypatch.setattr(session_presence, "human_display_name", names)
    sid = await start(transport)
    await service.set_team_control(ALICE, sid, True)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await service.submit_command(BOB, None, sid, command(service))
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    commands = [body for _, body in transport[3] if body["type"] == "local.session.command"]
    assert commands[0]["command"]["roomId"] == ""
    assert commands[0]["actor"] == {"kind": "human", "handle": None, "displayName": "Isol8 Admin"}
    # A failed lookup answers the bare id: no label, never an opaque id in the terminal.
    names.return_value = BOB.user_id
    assert await relay_module._actor_label({"roomId": "", "actorUserId": BOB.user_id}) is None


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
async def test_ingestion_is_bounded_ordered_deduplicated_and_emits_only_workspace_hints(transport, db):
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
        # One workspace hint on the workspace sessions channel; no per-room fan-out (2026-09-24).
        assert hint["channel"].endswith("/sessions")
        assert event["type"] == "workspace.sessions.changed"
        assert event["payload"]["humanUserId"] == ALICE.user_id
        assert "private transcript" not in json.dumps(hint)


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
async def test_two_workers_deliver_in_command_order_and_receipts_are_durable(transport, monkeypatch):
    sid = await start(transport)
    # Frozen clock, as in test_uncertain_delivery_never_replays_on_reconnect: the dispatch lease is
    # 5 wall-clock seconds, and a slow CI runner can spend it on moto round-trips.
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
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
async def test_uncertain_delivery_never_replays_on_reconnect(transport, monkeypatch, network_result):
    sid = await start(transport)
    # Frozen clock: the dispatch lease is 5 wall-clock seconds, and a slow CI runner spent
    # it on moto round-trips -- the command was rejected before any post (0 calls).
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
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
async def test_offline_human_send_waits_for_verified_successor_and_keeps_one_request(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    receipt = await service.submit_command(ALICE, None, sid, command(service))
    assert receipt.state == "pending" and receipt.deliver_by - receipt.created_at == 24 * 3600
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, receipt.request_id))["state"] == "pending"
    assert not [body for _, body in transport[3] if body["type"] == "local.session.command"]

    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-2", status="idle"), "previousGeneration": "generation-1"}
    )
    commands = [body["command"] for _, body in transport[3] if body["type"] == "local.session.command"]
    assert len(commands) == 1
    assert commands[0]["requestId"] == receipt.request_id
    assert commands[0]["expectedGeneration"] == "generation-2"
    assert (await repo.get_command(OWNER, sid, receipt.request_id))["state"] == "dispatched"
    assert (await service.submit_command(ALICE, None, sid, command(service))).request_id == receipt.request_id


@pytest.mark.asyncio
async def test_queued_send_leaves_with_a_fresh_window_and_a_stale_one_still_expires(transport):
    # Prod 2026-10-03: the Mac's Journal::admit refuses deliverBy - createdAt > 30, so a
    # follow-up queued with a 24 h deliverBy was "Not delivered" after every reconnect.
    sid = await start(transport)
    offline = {"Key": {"pk": repo.session_pk(OWNER, sid), "sk": "META"}, "UpdateExpression": "SET onlineUntil = :past"}
    messaging_repo._table().update_item(**offline, ExpressionAttributeValues={":past": int(time.time()) - 1})
    queued = await service.submit_command(ALICE, None, sid, command(service))
    stale = await service.submit_command(ALICE, None, sid, command(service, requestId="stale", text="later"))
    assert queued.deliver_by - queued.created_at == 24 * 3600

    connect(transport, "conn-2", "2000")
    later = time.time() + 600
    with patch("time.time", return_value=later):
        await relay(transport).handle_message("conn-2", registration(status="idle"))
    sent = [body["command"] for _, body in transport[3] if body["type"] == "local.session.command"]
    assert [c["requestId"] for c in sent] == [queued.request_id]
    assert sent[0]["createdAt"] == int(later) and sent[0]["deliverBy"] - sent[0]["createdAt"] <= 30
    assert "queuedAt" not in sent[0]
    row = await repo.get_command(OWNER, sid, queued.request_id)
    assert row["state"] == "dispatched" and row["queuedAt"] == queued.created_at

    with patch("time.time", return_value=stale.deliver_by + 1):
        await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "stale"))["state"] == "rejected"

    # Any long-lived command gets the same window, not only offline sends (e.g. a resume).
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": row["pk"], "sk": row["sk"]},
        UpdateExpression="SET #s = :a",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":a": "applied"},
    )
    now = int(time.time())
    plain = {k: v for k, v in row.items() if k not in {"offlineIdentity", "queuedAt", "connectionId", "claimUntil"}}
    plain.update(
        sk="COMMAND#plain",
        requestId="plain",
        state="pending",
        createdAt=now,
        deliverBy=now + 3600,
        commandSeq=row["commandSeq"] + 2,
    )
    table.put_item(Item=plain)
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    sent = [body["command"] for _, body in transport[3] if body["type"] == "local.session.command"]
    assert [c["requestId"] for c in sent] == ["plain"] and sent[0]["deliverBy"] - sent[0]["createdAt"] <= 30
    assert (await repo.get_command(OWNER, sid, "plain"))["queuedAt"] == now


@pytest.mark.asyncio
async def test_offline_teammate_send_is_rejected_when_successor_resets_control(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past, teamControl = :enabled",
        ExpressionAttributeValues={":past": int(time.time()) - 1, ":enabled": True},
    )
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await service.submit_command(BOB, None, sid, command(service))
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-2", status="idle"), "previousGeneration": "generation-1"}
    )
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "rejected"
    assert not [body for _, body in transport[3] if body["type"] == "local.session.command"]


@pytest.mark.asyncio
async def test_offline_teammate_send_is_rejected_after_workspace_revocation(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past, teamControl = :enabled",
        ExpressionAttributeValues={":past": int(time.time()) - 1, ":enabled": True},
    )
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await service.submit_command(BOB, None, sid, command(service))
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", registration())
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "rejected"
    assert not [body for _, body in transport[3] if body["type"] == "local.session.command"]


@pytest.mark.asyncio
async def test_offline_send_does_not_reactivate_removed_teammate(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past, teamControl = :enabled",
        ExpressionAttributeValues={":past": int(time.time()) - 1, ":enabled": True},
    )
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    with pytest.raises(ApiProblem, match="workspace"):
        await service.submit_command(BOB, None, sid, command(service))
    assert not (await messaging_repo.get_user_stream(OWNER, BOB.user_id))["active"]


@pytest.mark.asyncio
async def test_offline_send_expires_on_receipt_read_without_a_reconnect(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    sent = await service.submit_command(ALICE, None, sid, command(service))
    with patch("time.time", return_value=sent.deliver_by + 1):
        status = await service.get_command(ALICE, None, sid, sent.request_id)
    assert status.state == "rejected"
    assert (await repo.get_command(OWNER, sid, sent.request_id))["state"] == "rejected"


@pytest.mark.asyncio
async def test_offline_queue_excludes_live_controls(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    with pytest.raises(ApiProblem, match="offline"):
        await service.submit_command(ALICE, None, sid, command(service, kind="interrupt", text=None))


@pytest.mark.asyncio
async def test_offline_backlog_is_bounded_per_session(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    with patch.object(service, "OFFLINE_SEND_PENDING_LIMIT", 2):
        first = await service.submit_command(ALICE, None, sid, command(service, requestId="offline-1"))
        await service.submit_command(ALICE, None, sid, command(service, requestId="offline-2"))
        with pytest.raises(ApiProblem, match="too many"):
            await service.submit_command(ALICE, None, sid, command(service, requestId="offline-3"))
        with patch("time.time", return_value=first.deliver_by + 1):
            await service.get_command(ALICE, None, sid, first.request_id)
        await service.submit_command(ALICE, None, sid, command(service, requestId="offline-3"))


@pytest.mark.asyncio
async def test_offline_outbox_is_sender_private_and_survives_reopen(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service, text="resume when ready"))
    outbox = await service.list_offline_outbox(ALICE, sid)
    assert [(item.request_id, item.text, item.state) for item in outbox.commands] == [
        ("request-1", "resume when ready", "pending")
    ]
    assert (await service.list_offline_outbox(BOB, sid)).commands == []
    queued = await repo.get_command(OWNER, sid, "request-1")
    await repo.replace_command(queued, {**queued, "state": "applied"})
    assert [(item.request_id, item.state) for item in (await service.list_offline_outbox(ALICE, sid)).commands] == [
        ("request-1", "applied")
    ]


@pytest.mark.asyncio
async def test_offline_retry_lineage_hides_only_its_original_and_restores_on_rejection(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service, text="continue"))
    original = await repo.get_command(OWNER, sid, "request-1")
    await repo.replace_command(original, {**original, "state": "rejected"})

    await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="retry-a", text="continue"),
        replaces_request_id="request-1",
    )
    original = await repo.get_command(OWNER, sid, "request-1")
    retry = await repo.get_command(OWNER, sid, "retry-a")
    assert original["replacedByRequestId"] == "retry-a"
    assert retry["replacesRequestId"] == "request-1"
    assert [item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands] == ["retry-a"]
    with pytest.raises(ApiProblem, match="retry is already"):
        await service.submit_command(
            ALICE,
            None,
            sid,
            command(service, requestId="retry-b", text="continue"),
            replaces_request_id="request-1",
        )

    await repo.replace_command(retry, {**retry, "state": "rejected"})
    assert [item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands] == ["request-1"]
    await service.submit_command(ALICE, None, sid, command(service, requestId="unrelated", text="continue"))
    assert {item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands} == {
        "request-1",
        "unrelated",
    }
    await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="retry-c", text="continue"),
        replaces_request_id="request-1",
    )
    assert (await repo.get_command(OWNER, sid, "request-1"))["replacedByRequestId"] == "retry-c"
    assert "request-1" not in {item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands}


@pytest.mark.asyncio
async def test_online_retry_stays_in_outbox_until_its_outcome_is_known(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service, text="continue"))
    original = await repo.get_command(OWNER, sid, "request-1")
    await repo.replace_command(original, {**original, "state": "rejected"})
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :future",
        ExpressionAttributeValues={":future": int(time.time()) + 60},
    )

    await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="retry-online", text="continue"),
        replaces_request_id="request-1",
    )
    retry = await repo.get_command(OWNER, sid, "retry-online")
    assert "offlineIdentity" not in retry
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "COMMAND#request-1"},
        UpdateExpression="SET createdAt = :old",
        ExpressionAttributeValues={":old": int(time.time()) - 2 * service.OFFLINE_SEND_SECONDS - 1},
    )
    assert [item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands] == ["retry-online"]
    await repo.replace_command(retry, {**retry, "state": "rejected"})
    assert [item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands] == ["request-1"]

    await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="retry-online-2", text="continue"),
        replaces_request_id="request-1",
    )
    retry = await repo.get_command(OWNER, sid, "retry-online-2")
    await repo.replace_command(retry, {**retry, "state": "unknown", "deliverBy": int(time.time()) - 1})
    assert [(item.request_id, item.state) for item in (await service.list_offline_outbox(ALICE, sid)).commands] == [
        ("retry-online-2", "unknown")
    ]
    retry = await repo.get_command(OWNER, sid, "retry-online-2")
    await repo.replace_command(retry, {**retry, "state": "applied"})
    assert "request-1" not in {item.request_id for item in (await service.list_offline_outbox(ALICE, sid)).commands}


@pytest.mark.asyncio
async def test_offline_replacement_accepts_a_fenced_busy_turn(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service, text="continue"))
    original = await repo.get_command(OWNER, sid, "request-1")
    await repo.replace_command(original, {**original, "state": "rejected"})
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, capabilities={"sendMode": "queue", "steer": True, "interrupt": False})
    )
    retry = await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="retry-busy", text="continue", expectedTurnId="turn-1"),
        replaces_request_id="request-1",
    )
    assert retry.state == "pending"


@pytest.mark.asyncio
async def test_offline_outbox_keeps_new_pending_ahead_of_expired_unknowns(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service))
    pending = await repo.get_command(OWNER, sid, "request-1")
    table.update_item(
        Key={"pk": pending["pk"], "sk": pending["sk"]},
        UpdateExpression="SET commandSeq = :seq",
        ExpressionAttributeValues={":seq": 21},
    )
    for seq in range(1, 21):
        old = {
            **pending,
            "sk": f"COMMAND#old-{seq}",
            "requestId": f"old-{seq}",
            "commandSeq": seq,
            "state": "unknown",
            "createdAt": int(time.time()) - 3600,
            "deliverBy": int(time.time()) - 1,
        }
        table.put_item(Item=old)
    outbox = await service.list_offline_outbox(ALICE, sid)
    assert any(item.request_id == "request-1" and item.state == "pending" for item in outbox.commands)


@pytest.mark.asyncio
async def test_cancel_offline_send_fences_a_concurrent_dispatch_claim(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    await service.submit_command(ALICE, None, sid, command(service))
    original = await repo.get_command(OWNER, sid, "request-1")
    claim = {**original, "claimUntil": int(time.time()) + 5, "revision": "claimed"}
    await repo.replace_command(original, claim)
    with patch("core.services.local_session_relay.publish_resources_changed", new_callable=AsyncMock) as notice:
        cancelled = await service.cancel_offline_command(ALICE, sid.upper(), "request-1")
    notice.assert_awaited_once_with(OWNER, ALICE.user_id, session_id=sid)
    assert cancelled.state == "rejected" and cancelled.cancelled_at
    assert (await service.cancel_offline_command(ALICE, sid, "request-1")).cancelled_at == cancelled.cancelled_at
    with pytest.raises(ClientError):
        await repo.replace_command(claim, {**claim, "state": "dispatched"})
    await relay(transport).dispatch_session(OWNER, sid)
    assert not [body for _, body in transport[3] if body["type"] == "local.session.command"]


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
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
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
        first = [h for h in transport[4] if h["channel"].endswith("/sessions")]
        assert len(first) == 1
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b2", "afterSeq": 1, "events": [lifecycle]})
        assert len([h for h in transport[4] if h["channel"].endswith("/sessions")]) == 1
    with patch("time.time", return_value=2000000002.0):
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b3", "afterSeq": 2, "events": [lifecycle]})
    assert len([h for h in transport[4] if h["channel"].endswith("/sessions")]) == 2


@pytest.mark.asyncio
async def test_router_dispatches_post_but_receipt_get_is_read_only(transport):
    from core.auth import get_current_user, get_human_session
    from routers.messaging import router

    sid = await start(transport)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: ALICE
    with patch("core.services.local_session_relay.get_local_session_relay", return_value=relay(transport)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = f"/messaging/sessions/{sid}/commands"
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
async def test_unknown_command_is_not_reconciled_on_every_heartbeat(transport):
    """Each ask makes the Mac verify the native process (seconds of `lsof` and transcript reads).
    Asked every 10 s for a command that never settles, that took a Mac offline (2026-10-07)."""
    sid = await start(transport)
    await service.submit_command(BOB, ROOM, sid, command(service))
    with patch.object(transport[2], "send_message_async", return_value=False):
        await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "unknown"
    asked = [{"sessionId": sid, "requestId": "request-1", "generation": "generation-1", "state": "unknown"}]
    assert (await relay(transport).handle_message("conn-1", heartbeat(sid)))["reconcile"] == asked
    assert (await relay(transport).handle_message("conn-1", heartbeat(sid)))["reconcile"] == []
    # Measured from the second the ask was stamped, not from "now": the stamp is a whole second,
    # so a clock that ticked over since made "now + 59" a full 60 s later (a CI failure, 2026-10-09).
    asked_at = (await repo.get_command(OWNER, sid, "request-1"))["reconcileAskedAt"]
    with patch("time.time", return_value=asked_at + 59.99):
        assert (await relay(transport).handle_message("conn-1", heartbeat(sid)))["reconcile"] == []
    with patch("time.time", return_value=asked_at + 60):
        assert (await relay(transport).handle_message("conn-1", heartbeat(sid)))["reconcile"] == asked
    # The ask is bookkeeping: the command is still the unknown one its receipt will settle.
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "unknown"


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
async def test_device_heartbeat_overlaps_sessions_up_to_a_limit_but_never_their_lease_writes(transport):
    """Each lease write is a transaction that condition-checks the same pairing, enrollment and
    connection rows, and DynamoDB cancels one that overlaps another on any item (Moto does not).
    Overlapping them reran a third of prod heartbeats and dropped the Mac's connection."""
    first = await start(transport)
    host = relay(transport)
    sids = [first]
    for index in range(1, 8):
        ack = await host.handle_message("conn-1", registration(nativeConversationId=f"thread-{index + 1}"))
        sids.append(ack["sessionId"])
    frame = heartbeat(first)
    frame["sessions"] = [heartbeat(sid)["sessions"][0] for sid in sids]
    real_put, real_dispatch = repo.put_session, host.dispatch_session
    entered, release = asyncio.Event(), asyncio.Event()
    writing = most_writing = delivering = most_delivering = 0
    written = []

    async def counted_write(updated, *args, **kwargs):
        nonlocal writing, most_writing
        writing += 1
        most_writing = max(most_writing, writing)
        await asyncio.sleep(0)  # let any sibling that could start a write start it
        try:
            return await real_put(updated, *args, **kwargs)
        finally:
            written.append(updated["sessionId"])
            writing -= 1

    async def slow_delivery(owner, sid):
        nonlocal delivering, most_delivering
        delivering += 1
        most_delivering = max(most_delivering, delivering)
        if delivering == 4:
            entered.set()
        await release.wait()
        delivering -= 1
        return await real_dispatch(owner, sid)

    with patch.object(repo, "put_session", counted_write), patch.object(host, "dispatch_session", slow_delivery):
        task = asyncio.create_task(host.handle_message("conn-1", frame))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
        finally:
            release.set()
            response = await task
    assert response["type"] == "local.device.heartbeat.ack"
    assert response["rejected"] == []
    assert most_delivering == 4
    assert most_writing == 1
    assert sorted(written) == sorted(sids)


@pytest.mark.asyncio
async def test_a_heartbeat_that_loses_every_revision_race_is_the_relays_failure_not_a_conflict(transport):
    """A conflict on a heartbeat names no session: the Mac cannot quarantine one and ended its
    whole connection (prod 2026-10-08, 13 times). The cancellation leaves as it is, which the
    route answers INTERNAL, and the Mac sends its next heartbeat."""
    sid = await start(transport)
    cancelled = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
    with patch.object(repo, "put_session", AsyncMock(side_effect=cancelled)) as put:
        with pytest.raises(ClientError) as raised:
            await relay(transport).handle_message("conn-1", heartbeat(sid))
    assert raised.value is cancelled
    assert put.await_count == 18  # three tries a session, six runs of the frame
    # Every other frame names its session, so the Mac quarantines that one alone.
    with patch.object(repo, "put_session", AsyncMock(side_effect=cancelled)):
        with pytest.raises(ApiProblem) as conflict:
            await relay(transport).handle_message("conn-1", registration())
    assert conflict.value.error.code == "LOCAL_SESSION_CONFLICT"


@pytest.mark.asyncio
async def test_device_heartbeat_keeps_duplicate_session_updates_in_order(transport):
    sid = await start(transport)
    frame = heartbeat(sid)
    frame["sessions"] = [heartbeat(sid, status="busy")["sessions"][0], heartbeat(sid, status="idle")["sessions"][0]]
    real_put = repo.put_session
    first_started, release = asyncio.Event(), asyncio.Event()
    statuses = []
    second_before_release = False

    async def ordered_put(updated, *args, **kwargs):
        nonlocal second_before_release
        statuses.append(updated["status"])
        if len(statuses) == 1:
            first_started.set()
            await release.wait()
        elif not release.is_set():
            second_before_release = True
        return await real_put(updated, *args, **kwargs)

    with patch.object(repo, "put_session", ordered_put):
        task = asyncio.create_task(relay(transport).handle_message("conn-1", frame))
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
        finally:
            release.set()
            await task
    assert statuses == ["busy", "idle"]
    assert not second_before_release
    assert (await repo.get_session(OWNER, sid))["status"] == "idle"


@pytest.mark.asyncio
async def test_device_heartbeat_drains_workers_and_preserves_the_original_error(transport):
    first = await start(transport)
    host = relay(transport)
    second = (await host.handle_message("conn-1", registration(nativeConversationId="thread-2")))["sessionId"]
    frame = heartbeat(first)
    frame["sessions"].append(heartbeat(second, status="idle")["sessions"][0])
    real_put = repo.put_session
    second_started, release = asyncio.Event(), asyncio.Event()

    async def failing_write(updated, *args, **kwargs):
        if updated["sessionId"] == first:
            raise RuntimeError("original write failed")
        second_started.set()
        await release.wait()
        return await real_put(updated, *args, **kwargs)

    with patch.object(repo, "put_session", failing_write):
        task = asyncio.create_task(host.handle_message("conn-1", frame))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(RuntimeError, match="original write failed"):
                await task
    assert (await repo.get_session(OWNER, second))["status"] == "idle"


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
async def test_codex_live_controls_require_verified_heartbeat_and_exact_active_turn(transport):
    sid = await start(transport)
    with pytest.raises(ApiProblem):
        await service.submit_command(ALICE, None, sid, command(service, expectedTurnId="turn-1"))
    frame = heartbeat(sid, capabilities={"sendMode": "queue", "steer": True, "interrupt": True})
    await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["capabilities"] == {"sendMode": "queue", "steer": True, "interrupt": True}
    assert (
        await service.submit_command(ALICE, None, sid, command(service, expectedTurnId="turn-1"))
    ).state == "pending"
    assert (
        await service.submit_command(
            ALICE, None, sid, command(service, requestId="stop-1", kind="interrupt", text=None, expectedTurnId="turn-1")
        )
    ).state == "pending"
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    with pytest.raises(ApiProblem):
        await service.submit_command(
            ALICE, None, sid, command(service, requestId="late-steer", expectedTurnId="turn-1")
        )
    claude = await relay(transport).handle_message(
        "conn-1", registration(provider="claude", nativeConversationId="claude-1")
    )
    response = await relay(transport).handle_message(
        "conn-1", heartbeat(claude["sessionId"], capabilities={"sendMode": "queue", "steer": True, "interrupt": True})
    )
    assert response["rejected"] == [claude["sessionId"]]


@pytest.mark.asyncio
async def test_unknown_stop_settles_only_after_its_turn_ends_then_unblocks_next_stop(transport):
    sid = await start(transport)
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, capabilities={"sendMode": "queue", "steer": True, "interrupt": True})
    )
    await service.submit_command(
        ALICE, None, sid, command(service, requestId="old-stop", kind="interrupt", text=None, expectedTurnId="turn-1")
    )
    with patch.object(transport[2], "send_message_async", return_value=False):
        await relay(transport).dispatch_session(OWNER, sid)
    old = await repo.get_command(OWNER, sid, "old-stop")
    assert old["state"] == "unknown"
    settlement = {
        "type": "local.session.receipt",
        "sessionId": sid,
        "generation": "generation-1",
        "requestId": "old-stop",
        "outcome": "unknown",
        "providerMessageId": None,
        "held": False,
        "settled": True,
    }
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", settlement)
    assert not (await repo.get_command(OWNER, sid, "old-stop")).get("settled")

    await relay(transport).handle_message("conn-1", heartbeat(sid, activeTurnId="turn-2"))
    await service.submit_command(
        ALICE, None, sid, command(service, requestId="next-stop", kind="interrupt", text=None, expectedTurnId="turn-2")
    )
    transport[3].clear()
    await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, "next-stop"))["state"] == "pending"
    assert transport[3] == []

    await relay(transport).handle_message("conn-1", settlement)
    old = await repo.get_command(OWNER, sid, "old-stop")
    assert old["state"] == "unknown" and old["settled"] is True
    assert (await repo.get_command(OWNER, sid, "next-stop"))["state"] == "dispatched"
    assert transport[3][-1][1]["command"]["requestId"] == "next-stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["status", "active-turn", "control", "workspace", "network-id", "expired"])
async def test_visible_heartbeat_change_or_recovery_notifies_resources(transport, change):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    row = await repo.get_session(OWNER, sid)
    changes = {
        "status": {"status": "idle"},
        "active-turn": {"activeTurnId": "turn-2"},
        "control": {"controlUnavailableReason": "endpoint-unavailable"},
        "workspace": {"workspace": {"revision": 1, "coverage": "bounded", "changed": True, "overlap": False}},
        "network-id": {"networkSessionId": NETWORK_SID},
    }
    transport[4].clear()
    with patch("time.time", return_value=row["onlineUntil"] + 1 if change == "expired" else 10010):
        response = await relay(transport).handle_message("conn-1", heartbeat(sid, **changes.get(change, {})))
    assert response["type"] == "local.device.heartbeat.ack"
    assert transport[4]
    # A workspace scan or a control change is worth a refetch, but it is not the session doing something.
    after = await repo.get_session(OWNER, sid)
    assert after["lastEventAt"] == (10010 if change in {"status", "active-turn"} else 10000)


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
    assert any(hint["channel"].endswith("/sessions") for hint in transport[4])
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", registration())
    assert (await repo.get_session(OWNER, sid))["title"] == "Renamed by the harness"
    # A new process on the same live conversation continues its run: its title stays.
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await repo.get_session(OWNER, sid))["title"] == "Renamed by the harness"
    # A run after an end is a new one: it starts with no title until it says one.
    await relay(transport).handle_message(
        "conn-2", {**fixture("session-ended"), "sessionId": sid, "generation": "generation-2", "endedAt": 10005}
    )
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-3"), "previousGeneration": "generation-2"}
    )
    assert (await repo.get_session(OWNER, sid)).get("title") is None


@pytest.mark.asyncio
async def test_attention_registers_updates_on_heartbeat_clears_when_absent_and_hides_when_not_live(transport):
    assert "sessionAttention" in importlib.import_module("core.services.local_session_relay").FEATURES
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    # An older desktop sends none of them: nothing is stored, nothing is listed.
    sid = (await relay(transport).handle_message("conn-1", registration()))["sessionId"]
    stored = await repo.get_session(OWNER, sid)
    assert not {"done", "waitingFor", "note"} & stored.keys()
    await relay(transport).handle_message("conn-1", registration(note="run lightbulb login, then press Resume"))
    listed = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert (listed.note, listed.waiting_for, listed.done) == ("run lightbulb login, then press Resume", None, None)
    assert "waitingFor" not in listed.model_dump(by_alias=True), "absent, never a null"
    transport[4].clear()
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="waiting-approval", waitingFor="permission prompt")
    )
    stored = await repo.get_session(OWNER, sid)
    assert stored["waitingFor"] == "permission prompt" and "note" not in stored, "each frame says all of it"
    assert any(hint["channel"].endswith("/sessions") for hint in transport[4])
    await relay(transport).handle_message("conn-1", heartbeat(sid, done=True))
    stored = await repo.get_session(OWNER, sid)
    assert stored["done"] is True and "waitingFor" not in stored
    stored["onlineUntil"] = 0
    assert service._session_public(stored).done is None, "an offline session is waiting on nothing"


@pytest.mark.asyncio
async def test_signals_register_update_on_heartbeat_clear_when_absent_and_hide_when_not_live(transport):
    assert "sessionSignals" in importlib.import_module("core.services.local_session_relay").FEATURES
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    # An older desktop sends none of them: nothing is stored, nothing is listed.
    sid = (await relay(transport).handle_message("conn-1", registration()))["sessionId"]
    stored = await repo.get_session(OWNER, sid)
    assert not {"changes", "agents", "pr"} & stored.keys()
    listed = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert not {"changes", "agents", "pr"} & listed.model_dump(by_alias=True).keys(), "absent, never a null"
    pr = {"number": 123, "state": "open", "url": "https://github.com/o/r/pull/123", "checks": "failing"}
    transport[4].clear()
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, changes={"files": 3, "additions": 12, "deletions": 4}, agents=2, pr=pr)
    )
    listed = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert listed.changes.files == 3 and listed.agents == 2 and listed.pr.checks == "failing"
    assert listed.model_dump(by_alias=True)["pr"] == pr
    assert any(hint["channel"].endswith("/sessions") for hint in transport[4]), "a moved signal is news"
    # No checks yet: the field is absent, not a null.
    await relay(transport).handle_message("conn-1", heartbeat(sid, pr={**pr, "checks": None}))
    stored = await repo.get_session(OWNER, sid)
    assert "checks" not in stored["pr"] and not {"changes", "agents"} & stored.keys(), "each frame says all of it"
    stored["onlineUntil"] = 0
    assert service._session_public(stored).pr is None, "an offline session shows no PR"
    with pytest.raises(pydantic.ValidationError):
        importlib.import_module("core.services.local_session_relay").HeartbeatSession(
            sessionId=sid, generation="g", status="idle", pr={**pr, "url": "javascript:x"}
        )


NETWORK_SID = "abc123def456/claude:11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_network_session_id_registers_rides_heartbeats_survives_silence_and_is_validated(transport):
    assert "networkSessionId" in importlib.import_module("core.services.local_session_relay").FEATURES
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    sid = (await relay(transport).handle_message("conn-1", registration()))["sessionId"]
    assert "networkSessionId" not in await repo.get_session(OWNER, sid), "absent, never a stored null"
    plain = (await service.list_resources(BOB, ROOM)).sessions[0].model_dump(by_alias=True)
    assert "networkSessionId" not in plain
    await relay(transport).handle_message("conn-1", heartbeat(sid, networkSessionId=NETWORK_SID))
    assert (await repo.get_session(OWNER, sid))["networkSessionId"] == NETWORK_SID
    await relay(transport).handle_message("conn-1", heartbeat(sid))
    assert (await repo.get_session(OWNER, sid))["networkSessionId"] == NETWORK_SID, "silence keeps it"
    await relay(transport).handle_message("conn-1", registration())
    assert (await repo.get_session(OWNER, sid))["networkSessionId"] == NETWORK_SID, "a re-register keeps it"
    other = NETWORK_SID.replace("1111", "2222")
    await relay(transport).handle_message("conn-1", registration(networkSessionId=other))
    listed = (await service.list_resources(BOB, ROOM)).sessions[0].model_dump(by_alias=True)
    assert listed["networkSessionId"] == other
    assert not {"nativeConversationId", "profileFingerprint"} & listed.keys()
    for bad in ("nope", "a/b", NETWORK_SID + "!"):
        with pytest.raises(ApiProblem):
            await relay(transport).handle_message("conn-1", registration(networkSessionId=bad))
        with pytest.raises(pydantic.ValidationError):
            importlib.import_module("core.services.local_session_relay").HeartbeatSession(
                sessionId=sid, generation="g", status="idle", networkSessionId=bad
            )


@pytest.mark.asyncio
async def test_every_session_registers_and_a_holder_only_labels_it(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    plain = registration()
    del plain["session"]["holderId"]
    sid = (await relay(transport).handle_message("conn-1", plain))["sessionId"]
    assert "holderId" not in await repo.get_session(OWNER, sid), (
        "a plain terminal session is tracked (rulings 2026-10-04)"
    )
    await relay(transport).handle_message("conn-1", registration(holderId=None))
    assert "holderId" not in await repo.get_session(OWNER, sid), "null is no holder, never a stored null"
    await relay(transport).handle_message("conn-1", registration())
    assert (await repo.get_session(OWNER, sid))["holderId"] == "ab12cd34"
    rerun = {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    del rerun["session"]["holderId"]
    await relay(transport).handle_message("conn-1", rerun)
    assert "holderId" not in await repo.get_session(OWNER, sid), "a new run outside a holder loses the label"
    for body in (registration(holderId="../x"), registration(kind="background")):
        with pytest.raises(ApiProblem) as excinfo:
            await relay(transport).handle_message("conn-1", body)
        assert excinfo.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_a_legacy_background_row_is_an_ordinary_session_once_it_registers_again(transport, db):
    sid = await start(transport)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET kind = :k",
        ExpressionAttributeValues={":k": "background"},
    )
    await relay(transport).handle_message("conn-1", registration())
    assert "kind" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_a_session_paused_on_its_mac_registers_as_only_you(transport):
    sid = await start(transport)
    assert not (await repo.get_session(OWNER, sid)).get("unshared"), "absent leaves sharing as it is"
    rerun = {**registration(generation="generation-2", shared=False), "previousGeneration": "generation-1"}
    await relay(transport).handle_message("conn-1", rerun)
    row = await repo.get_session(OWNER, sid)
    assert row["unshared"] is True and "shared" not in row
    await relay(transport).handle_message("conn-1", registration(generation="generation-2"))
    assert (await repo.get_session(OWNER, sid))["unshared"] is True, "only the owner shares it again"
    # The owner shares it again from the web while the Mac is away: the same run reconnecting with
    # its stale pause does not take that back (the Mac follows presence instead).
    await service.set_sharing(ALICE, sid, True)
    await relay(transport).handle_message("conn-1", registration(generation="generation-2", shared=False))
    assert not (await repo.get_session(OWNER, sid)).get("unshared")


@pytest.mark.asyncio
async def test_a_fork_records_the_session_it_continues(transport):
    origin = await start(transport)
    fork = await relay(transport).handle_message(
        "conn-1", registration(nativeConversationId="thread-2", forkedFrom=origin)
    )
    assert (await repo.get_session(OWNER, fork["sessionId"]))["forkedFrom"] == origin
    assert "forkedFrom" not in await repo.get_session(OWNER, origin)


@pytest.mark.asyncio
async def test_a_fork_keeps_the_name_its_person_gave_the_origin(transport):
    origin = await start(transport)
    row = await repo.get_session(OWNER, origin)
    await repo.put_session(
        {**row, "displayTitle": "Release train"},
        row,
        await repo.get_enrollment(OWNER, row["humanUserId"], row["deviceId"]),
    )
    fork = await relay(transport).handle_message(
        "conn-1", registration(nativeConversationId="thread-2", forkedFrom=origin)
    )
    assert (await repo.get_session(OWNER, fork["sessionId"]))["displayTitle"] == "Release train"


@pytest.mark.asyncio
async def test_heartbeat_reports_the_terminal_and_only_a_running_session_shows_one(transport, db):
    sid = await start(transport)
    row = await repo.get_session(OWNER, sid)
    assert service._session_public(row, ALICE.user_id).terminal == "held", "held by its holderId until the Mac says"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="attach"))
    assert service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id).terminal == "attach"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none"))
    public = service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id)
    assert public.terminal is None and "terminal" not in public.model_dump(by_alias=True)
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="attach"))
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET endedAt = :t",
        ExpressionAttributeValues={":t": 1},
    )
    assert service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id).terminal is None


@pytest.mark.asyncio
async def test_a_new_run_forgets_the_old_runs_terminal(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="attach"))
    rerun = {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    del rerun["session"]["holderId"]
    await relay(transport).handle_message("conn-1", rerun)
    assert "terminal" not in await repo.get_session(OWNER, sid)


@pytest.mark.asyncio
async def test_the_relay_advertises_session_terminal(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    assert "sessionTerminal" in (await relay(transport).handle_message("conn-1", registration()))["features"]


@pytest.mark.asyncio
async def test_http_dispatch_failure_returns_durable_receipt_without_logging_text(transport, caplog):
    from core.auth import get_current_user, get_human_session
    from routers.messaging import router

    sid = await start(transport)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: ALICE
    with patch("core.services.local_session_relay.get_local_session_relay", side_effect=RuntimeError("secret-token")):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post(
                f"/messaging/sessions/{sid}/commands",
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

    async def ingest_before_authorization(*args, **kwargs):
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
        return await authorize(*args, **kwargs)

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
async def test_a_tool_call_with_a_float_argument_is_stored(transport):
    """A float anywhere in a tool's input or result (a screenshot's scale, a wait's seconds) used to
    make DynamoDB refuse the batch, so the Mac resent it forever and the session's history froze."""
    sid = await start(transport)
    call = {
        "kind": "tool",
        "role": "assistant",
        "text": "screenshot",
        "itemId": "toolu_02",
        "tool": {
            "name": "computer",
            "status": "done",
            "input": {"scale": 0.6, "region": [0, 0.5]},
            "result": {"took": 1.25},
        },
    }
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "f1",
        "afterSeq": 0,
        "events": [call],
    }
    ack = await relay(transport).handle_message("conn-1", body)
    assert ack["afterSeq"] == 1
    tool = (await service.list_events(BOB, ROOM, sid)).model_dump(by_alias=True, exclude_none=True)["events"][0]["tool"]
    assert tool["input"] == {"scale": 0.6, "region": [0, 0.5]} and tool["result"] == {"took": 1.25}


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
        response = await client.get(f"/messaging/sessions/{sid}/events")
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
async def test_device_heartbeat_retries_only_the_row_that_lost_its_revision_race(transport):
    module = importlib.import_module("core.services.local_session_relay")
    sids = [await start(transport)]
    for thread in ("thread-2", "thread-3"):
        registered = await relay(transport).handle_message("conn-1", registration(nativeConversationId=thread))
        sids.append(registered["sessionId"])
    frame = heartbeat(sids[0], status="idle")
    frame["sessions"] += [{**frame["sessions"][0], "sessionId": sid} for sid in sids[1:]]
    contested, real, puts = sids[1], repo.put_session, {sid: 0 for sid in sids}

    async def upload_wins_once(row, previous, enrollment, **kwargs):
        puts[row["sessionId"]] += 1
        if row["sessionId"] == contested and puts[contested] == 1:
            # An upload writes the same row between the heartbeat's read and its fenced write.
            await real(module._revised(previous), previous, enrollment)
        await real(row, previous, enrollment, **kwargs)

    with (
        patch.object(repo, "put_session", side_effect=upload_wins_once),
        patch.object(repo, "get_sessions", wraps=repo.get_sessions) as batched,
        patch.object(repo, "get_session", wraps=repo.get_session) as single,
    ):
        response = await relay(transport).handle_message("conn-1", frame)

    assert response["type"] == "local.device.heartbeat.ack" and response["rejected"] == []
    # One batched read for the frame and no per-session first read: each session's one single
    # read is delivery's own, and only the contested row's retry reads again.
    batched.assert_awaited_once_with(OWNER, sids)
    reads = [call.args[1] for call in single.await_args_list]
    assert {sid: reads.count(sid) for sid in sids} == {sids[0]: 1, contested: 2, sids[2]: 1}
    # The contested row is re-read and written again; the others are written once, so the
    # frame itself ran once.
    assert puts == {sids[0]: 1, contested: 2, sids[2]: 1}
    rows = [await repo.get_session(OWNER, sid) for sid in sids]
    assert [row["status"] for row in rows] == ["idle"] * 3


@pytest.mark.asyncio
async def test_device_heartbeat_never_rejects_on_the_batched_row_alone(transport):
    sid = await start(transport)
    # The batch ran before this session was registered onto the connection: it read nothing.
    with patch.object(repo, "get_sessions", AsyncMock(return_value=[None])):
        response = await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert response["rejected"] == []
    assert (await repo.get_session(OWNER, sid))["status"] == "idle"


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
    # Ended before any event: unlisted (nothing to show or resume), still readable by id.
    assert (await service.get_session(BOB, sid)).status == "ended"
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
@pytest.mark.parametrize("reason", ["login-session-ended", "idle-stopped"])
async def test_ended_frame_reports_the_macs_own_end(transport, reason):
    """A Mac that ended a held session itself (a holder older than its login session, or one idle
    an hour with nobody on it) says why, and the row and the public session carry it."""
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    frame = {
        **fixture("session-ended"),
        "sessionId": sid,
        "generation": "generation-1",
        "endedAt": 10005,
        "reason": reason,
    }
    with patch("time.time", return_value=10006):
        await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["status"] == "ended"
    assert row["endedReason"] == reason
    assert (await service.get_session(BOB, sid)).ended_reason == reason


def test_relay_lists_ended_idle_stopped_so_the_mac_may_stop_idle_sessions():
    from core.services import local_session_relay as module

    assert "endedIdleStopped" in module.FEATURES


@pytest.mark.asyncio
async def test_local_session_analytics_tracks_verified_generation_once_without_content(transport):
    with patch("core.services.local_session_relay.capture_product_event") as capture:
        sid = await start(transport)
        await relay(transport).handle_message("conn-1", registration(title="private prompt", cwdName="/private/folder"))
        ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
        await relay(transport).handle_message("conn-1", ended)
        await relay(transport).handle_message("conn-1", ended)
    assert [call.kwargs["event"] for call in capture.call_args_list] == ["local_session_shared", "local_session_ended"]
    assert all(call.kwargs["distinct_id"] == ALICE.user_id for call in capture.call_args_list)
    assert all(call.kwargs["properties"]["workspace_id"] == OWNER for call in capture.call_args_list)
    assert all(call.kwargs["properties"]["provider"] == "codex" for call in capture.call_args_list)
    assert all(call.kwargs["properties"]["session_id"] == sid for call in capture.call_args_list)
    assert all("private" not in str(call.kwargs) for call in capture.call_args_list)


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
    # A reconnect re-registers the same run: that is not activity, so the time stays.
    with patch("time.time", return_value=10050):
        await relay(transport).handle_message("conn-1", registration())
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10020
    # Nor is the Mac becoming unsure of the status after a restart.
    with patch("time.time", return_value=10060):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="unknown"))
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10020
    # Nor is the restart's re-check settling it back to idle; a turn starting from unknown is activity.
    with patch("time.time", return_value=10070):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10020
    with patch("time.time", return_value=10080):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="unknown"))
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="busy"))
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10080
    # A reconnect that itself carries a turn ending is activity.
    with patch("time.time", return_value=10090):
        await relay(transport).handle_message("conn-1", registration(status="idle"))
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10090


def test_session_activity_is_a_turn_starting_or_ending():
    from core.services.local_sessions import session_activity

    assert session_activity("idle", "busy", None, None) and session_activity("busy", "idle", None, None)
    assert session_activity("unknown", "busy", None, None) and session_activity("idle", "idle", None, "t1")
    assert not session_activity("busy", "unknown", None, None) and not session_activity("idle", "offline", None, None)
    assert not session_activity("unknown", "idle", None, None) and not session_activity("idle", "idle", None, None)


@pytest.mark.asyncio
async def test_register_publishes_one_workspace_hint_whatever_the_member_count(transport, db):
    """One publish on /ws/<ws>/sessions, not one per member stream (dev 2026-10-08: 51
    members x 30 registrations was 1,530 publishes)."""
    for i in range(50):
        await messaging_repo.ensure_user_stream(OWNER, f"user_{i}")
    workspace = await messaging_repo.ensure_workspace_id(OWNER)
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    connect(transport)
    transport[4].clear()
    await relay(transport).handle_message("conn-1", registration())
    hints = [h for h in transport[4] if "workspace.sessions.changed" in h["events"][0]]
    assert [h["channel"] for h in hints] == [f"/ws/{workspace}/sessions"]
    assert json.loads(hints[0]["events"][0])["payload"] == {"humanUserId": ALICE.user_id}


@pytest.mark.asyncio
async def test_event_batches_send_a_transcript_workspace_hint(transport, db):
    """A streaming batch reaches open panes as a transcript-marked workspace hint, which the
    sidebar list ignores; there is no per-room hint any more."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    sid = await start(transport)
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
    hints = [json.loads(h["events"][0]) for h in transport[4] if h["channel"].endswith("/sessions")]
    assert len(hints) == 1
    assert all(e["payload"] == {"humanUserId": ALICE.user_id, "transcript": True, "sessionId": sid} for e in hints)
    assert not any("/rooms/" in h["channel"] for h in transport[4])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["created", "replaced", "released", "unchanged"])
async def test_approval_metadata_changes_refresh_the_session_list_without_waiting_for_poll(transport, change):
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    with patch("time.time", return_value=2000000000.0):
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
        connect(transport)
        registered = await relay(transport).handle_message(
            "conn-1", registration(provider="claude", status="waiting-approval", activeTurnId=None)
        )
        sid = registered["sessionId"]
        frame = {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": "initial-approval",
            "afterSeq": 0,
            "events": [fixture("approval-pending-event")],
        }
        if change != "created":
            await relay(transport).handle_message("conn-1", frame)
        transport[4].clear()
        event = fixture("approval-pending-event")
        if change == "replaced":
            event["itemId"] = "appr-replacement"
            event["tool"]["input"]["inputDigest"] = "e" * 64
        elif change == "released":
            event = {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "approval_pending"}
        frame.update(batchId="approval-change", afterSeq=0 if change == "created" else 1, events=[event])
        await relay(transport).handle_message("conn-1", frame)

        public = await service.get_session(ALICE, sid)
        assert public.status == "waiting-approval"
        if change == "released":
            assert public.pending_approval is None
        else:
            assert public.pending_approval.approval_id == event["itemId"]
        hints = [json.loads(h["events"][0]) for h in transport[4] if h["channel"].endswith("/sessions")]
        assert len(hints) == 1
        # The hint names the one session, so clients re-read that row instead of the whole list.
        expected = {"humanUserId": ALICE.user_id, "sessionId": sid}
        if change == "unchanged":
            expected["transcript"] = True
        assert all(h["type"] == "workspace.sessions.changed" and h["payload"] == expected for h in hints)
        # Retrying the committed batch must not invalidate the list a second time.
        await relay(transport).handle_message("conn-1", frame)
        assert len(transport[4]) == len(hints)


@pytest.mark.asyncio
async def test_device_heartbeat_hints_once_per_frame(transport, monkeypatch):
    """Two lapsed mirrors in one frame each need a hint; the frame sends one, not one per session."""
    from unittest.mock import AsyncMock

    from core.services import local_session_relay as module

    with patch("time.time", return_value=10000):
        first = await start(transport)
        second = (await relay(transport).handle_message("conn-1", registration(nativeConversationId="thread-2")))[
            "sessionId"
        ]
    lapsed = (await repo.get_session(OWNER, first))["onlineUntil"] + 1
    frame = heartbeat(first, status="idle")
    frame["sessions"].append({**heartbeat(second, status="idle")["sessions"][0]})
    hints = AsyncMock()
    monkeypatch.setattr(module, "publish_resources_changed", hints)
    with patch("time.time", return_value=lapsed):
        response = await relay(transport).handle_message("conn-1", frame)
    assert response["rejected"] == []
    assert hints.await_count == 1


@pytest.mark.asyncio
async def test_paused_sharing_hides_the_session_and_drops_its_transcript_until_resumed(transport):
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    sid = await start(transport)

    def batch(n, after):
        return {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": f"batch-{n}",
            "afterSeq": after,
            "events": [{"kind": "text", "role": "assistant", "text": f"secret {n}", "truncated": False}] * 2,
        }

    with pytest.raises(ApiProblem) as denied:
        await service.set_sharing(BOB, sid, False)
    assert denied.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    paused = await service.set_sharing(ALICE, sid, False)
    assert paused.shared is False
    # Nobody else sees it, anywhere; its owner still does.
    assert [s.session_id for s in (await service.list_workspace_sessions(BOB)).sessions] == []
    assert [s.session_id for s in (await service.list_workspace_sessions(ALICE)).sessions] == [sid]
    for call in (service.get_session(BOB, sid), service.list_events(BOB, None, sid)):
        with pytest.raises(ApiProblem) as hidden:
            await call
        assert hidden.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    assert await service.live_sessions_for_room(OWNER, ROOM, [{"kind": "human", "id": ALICE.user_id}]) == []

    first = await relay(transport).handle_message("conn-1", batch(1, 0))
    # The ack says the batch was dropped, so the Mac keeps its events for when sharing resumes.
    assert (first["afterSeq"], first["paused"]) == (2, True)
    # A lost ack replays idempotently: the notice row carries the batch hash.
    assert await relay(transport).handle_message("conn-1", batch(1, 0)) == first
    assert (await relay(transport).handle_message("conn-1", batch(2, 2)))["afterSeq"] == 4
    resumed = await service.set_sharing(ALICE, sid, True)
    assert resumed.shared is True and resumed.latest_event_seq == 4
    assert "paused" not in await relay(transport).handle_message("conn-1", batch(3, 4))
    texts = [e.text for e in (await service.list_events(BOB, None, sid)).events]
    from core.services.local_session_relay import PAUSED_NOTICE

    assert texts == [PAUSED_NOTICE, "secret 3", "secret 3"]
    # Pausing again marks the new gap once more.
    await service.set_sharing(ALICE, sid, False)
    await relay(transport).handle_message("conn-1", batch(4, 6))
    assert (await service.list_events(ALICE, None, sid)).events[-1].text == PAUSED_NOTICE


@pytest.mark.asyncio
async def test_waiting_and_long_turn_finish_notify_the_owner_once(transport):
    from core.services import activity, local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **_):
        pushes.append((user_id, title, body, data))

    with patch("time.time", return_value=10000):
        sid = await start(transport)  # registers busy: the turn starts now
    frames = [
        (10010, {"status": "waiting-approval", "title": "Fix login"}),
        (10020, {"status": "waiting-approval"}),  # still waiting: no second notification
        (10030, {"status": "busy"}),
        (10100, {"status": "idle"}),  # 100 s turn: finished
        (10110, {"status": "busy"}),
        (10120, {"status": "idle"}),  # 10 s turn: too short to notify
    ]
    with patch("core.services.push_service.send_push_to_user", push):
        for stamp, change in frames:
            with patch("time.time", return_value=stamp):
                await relay(transport).handle_message("conn-1", heartbeat(sid, **change))
                await asyncio.gather(*module._tasks)
    assert [(p[0], p[1]) for p in pushes] == [(ALICE.user_id, "Codex · Fix login")] * 2
    assert pushes[0][2] == "Waiting for you: approve or answer it in your terminal"
    assert pushes[1][2].startswith("Finished")
    assert pushes[0][3] == {"source": "session", "sessionId": sid}
    attention = [h for h in transport[4] if json.loads(h["events"][0])["type"] == "workspace.session.attention"]
    assert [json.loads(h["events"][0])["payload"]["state"] for h in attention] == ["waiting", "finished"]

    # Activity: the owner's feed holds both; the waiting one pins while the session still waits.
    with patch("time.time", return_value=10200):
        page = await activity.list_activity(ALICE, cursor=None, limit=30)
    session_items = [i for i in page.items if i.kind == "session"]
    assert [(i.subtype, i.state) for i in session_items] == [("finished", "open"), ("waiting", "resolved")]
    assert session_items[0].actions[0].navigate.kind == "session" and session_items[0].actions[0].navigate.id == sid
    assert page.attention_count == 0  # a finished turn never badges; the wait is over

    with patch("core.services.push_service.send_push_to_user", push), patch("time.time", return_value=10300):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="waiting-approval"))
        await asyncio.gather(*module._tasks)
        page = await activity.list_activity(ALICE, cursor=None, limit=30)
    assert page.items[0].subtype == "waiting" and page.items[0].state == "open"
    assert page.attention_count == 1
    # Nobody else is told: Bob's feed has no session items.
    with patch("time.time", return_value=10300):
        bob = await activity.list_activity(BOB, cursor=None, limit=30)
    assert not [i for i in bob.items if i.kind == "session"]


@pytest.mark.asyncio
async def test_session_notification_prefs_choose_banner_badge_or_nothing_per_kind(transport):
    from core.repositories import user_repo
    from core.services import local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **kwargs):
        pushes.append((body, kwargs))

    await user_repo.put(ALICE.user_id, "alice@example.com")
    prefs = {"waiting": "badge", "finished": "off", "attention": "banner", "onlyUnfocused": True, "sound": False}
    await user_repo.set_prefs(ALICE.user_id, session_notifications=prefs)
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    frames = [
        (10010, {"status": "waiting-approval", "title": "Fix login"}),  # badge: no push
        (10030, {"status": "busy"}),
        (10100, {"status": "idle"}),  # a long turn, off: no push
        (10110, {"status": "idle", "note": "run lightbulb login, then press Resume"}),  # banner
        (10120, {"status": "idle", "note": "run lightbulb login, then press Resume"}),  # same note: once
    ]
    with patch("core.services.push_service.send_push_to_user", push):
        for stamp, change in frames:
            with patch("time.time", return_value=stamp):
                await relay(transport).handle_message("conn-1", heartbeat(sid, **change))
                await asyncio.gather(*module._tasks)
    assert pushes == []  # a hand-off note never notifies, even set to banner (owner 2026-10-27)
    events = [json.loads(h["events"][0]) for h in transport[4]]
    notices = [e["payload"] for e in events if e["type"] == "workspace.session.attention"]
    assert [(n["state"], n["level"], n["sound"], n["onlyUnfocused"]) for n in notices] == [
        ("waiting", "badge", False, True),
        ("finished", "off", False, True),
    ]

    # The defaults are the old behaviour: a note is never a banner, nor even a live notice.
    await user_repo.set_prefs(ALICE.user_id, session_notifications=dict(user_repo.SESSION_NOTIFICATION_DEFAULTS))
    pushes.clear()
    with patch("core.services.push_service.send_push_to_user", push), patch("time.time", return_value=10200):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", note="a new note"))
        await asyncio.gather(*module._tasks)
    assert pushes == []


@pytest.mark.asyncio
async def test_a_turn_that_ends_with_a_new_note_tells_only_the_finish(transport):
    from core.repositories import user_repo
    from core.services import local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **kwargs):
        pushes.append(body)

    await user_repo.put(ALICE.user_id, "alice@example.com")
    prefs = {**user_repo.SESSION_NOTIFICATION_DEFAULTS, "finished": "off", "attention": "banner"}
    await user_repo.set_prefs(ALICE.user_id, session_notifications=prefs)
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    with patch("core.services.push_service.send_push_to_user", push), patch("time.time", return_value=10100):
        # One frame: a 100 s turn ends AND the job leaves a note.
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", note="run lightbulb login"))
        await asyncio.gather(*module._tasks)
    assert pushes == []  # the note notifies nobody; only the finish is told, by its own setting
    notices = [json.loads(h["events"][0]) for h in transport[4]]
    notices = [n for n in notices if n["type"] == "workspace.session.attention"]
    assert [(n["payload"]["state"], n["payload"]["level"]) for n in notices] == [("finished", "off")]


@pytest.mark.asyncio
async def test_a_prefs_read_failure_sends_no_banner(transport):
    from core.repositories import user_repo
    from core.services import local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **kwargs):
        pushes.append(body)

    with patch("time.time", return_value=10000):
        sid = await start(transport)
    with (
        patch("core.services.push_service.send_push_to_user", push),
        patch.object(user_repo, "get", AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch("time.time", return_value=10010),
    ):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="waiting-approval"))
        await asyncio.gather(*module._tasks)
    assert pushes == []
    notices = [json.loads(h["events"][0]) for h in transport[4]]
    levels = [n["payload"]["level"] for n in notices if n["type"] == "workspace.session.attention"]
    assert levels == ["badge"]


async def test_subagent_events_round_trip_and_a_long_background_end_notifies_once(transport):
    from core.services import activity, local_session_relay as module

    assert "sessionSubagents" in module.FEATURES
    assert "pickupRequests" in module.FEATURES
    assert "approvalInput" in module.FEATURES
    pushes = []

    async def push(user_id, title, body, data=None, **_):
        pushes.append((user_id, title, body, data))

    def end(agent, status, **result):
        return {
            "kind": "lifecycle",
            "role": "system",
            "text": f'Agent "{agent}" {status}',
            "lifecycle": "subagent_end",
            "subagent": agent,
            "tool": {"name": "Agent", "status": "done", "result": result},
        }

    with patch("time.time", return_value=10000):
        sid = await start(transport)
    start_event = {
        "kind": "lifecycle",
        "role": "system",
        "text": "Survey the parser",
        "lifecycle": "subagent_start",
        "subagent": "a1",
        "parentId": "toolu_1",
    }
    inner = {"kind": "text", "role": "assistant", "text": "found it", "subagent": "a1"}
    events = [
        start_event,
        inner,
        end("a1", "finished", background=True, durationMs=90_000),
        end("quick", "finished", background=True, durationMs=5_000),  # too short, like a short turn
        end("fg", "finished", background=False, durationMs=90_000),  # foreground: the parent's turn
        end("failed", "failed", background=True),  # no duration: notifies
    ]
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "b1",
        "afterSeq": 0,
        "events": events,
    }

    async def send(stamp, message):
        with patch("core.services.push_service.send_push_to_user", push), patch("time.time", return_value=stamp):
            await relay(transport).handle_message("conn-1", message)
            await asyncio.gather(*module._tasks)

    def batch(batch_id, after, evts):
        return {**body, "batchId": batch_id, "afterSeq": after, "events": evts}

    # The parent is still in its turn (registered busy): its turn end speaks for them.
    await send(10100, body)
    await send(10100, body)  # a replayed batch notifies nothing either
    page = await service.list_events(BOB, ROOM, sid)
    stored = page.model_dump(by_alias=True, exclude_none=True)["events"]
    assert [e.get("subagent") for e in stored] == ["a1", "a1", "a1", "quick", "fg", "failed"]
    assert stored[0]["lifecycle"] == "subagent_start" and stored[0]["parentId"] == "toolu_1"
    assert pushes == []
    # The turn ends 5 s later -- short, but a subagent ended in it: ONE notice, the turn's own.
    await send(10105, heartbeat(sid, status="idle"))
    assert [p[2].startswith("Finished") for p in pushes] == [True]

    # Parent idle, two background subagents end in one batch: one notice, the last one's.
    await send(10200, batch("b2", 6, [end("b1", "finished", background=True), end("b2", "failed", background=True)]))
    assert [p[2] for p in pushes[1:]] == ['Agent "b2" failed']
    assert all(p[0] == ALICE.user_id and " · " in p[1] for p in pushes)  # who · session, body says what

    # A long turn finishes, and a subagent's end lands a second later: the same moment, one notice.
    await send(10300, heartbeat(sid, status="busy"))
    await send(10400, heartbeat(sid, status="idle"))
    await send(10401, batch("b3", 8, [end("c1", "finished", background=True)]))
    assert len(pushes) == 3 and pushes[2][2].startswith("Finished")

    with patch("time.time", return_value=10500):
        feed = await activity.list_activity(ALICE, cursor=None, limit=30)
    assert len([i for i in feed.items if i.kind == "session"]) == 3


@pytest.mark.asyncio
@pytest.fixture
def thread_hops(monkeypatch):
    """Every DynamoDB call, Management API post and AppSync signature is one job on the default
    thread pool (6 threads on 2 vCPU), which the whole backend process shares."""
    real, hops = asyncio.to_thread, []

    async def counted(func, *args, **kwargs):
        hops.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counted)
    return hops


@pytest.mark.asyncio
async def test_a_full_heartbeat_frame_costs_a_bounded_number_of_thread_pool_calls(transport, thread_hops):
    """24 sessions is one frame's cap; a Mac with 44 sends two. 3 calls admit the frame, 5 per
    session renew it and settle its commands, 1 per session reads its viewers, 4 fill the ack's
    lists. It was 198 (prod 2026-10-07: one Mac's heartbeats were a tenth of the table's reads)."""
    first = await start(transport)
    host = relay(transport)
    sids = [first]
    for index in range(1, 24):
        ack = await host.handle_message("conn-1", registration(nativeConversationId=f"thread-{index + 1}"))
        sids.append(ack["sessionId"])
    frame = heartbeat(first)
    frame["sessions"] = [heartbeat(sid)["sessions"][0] for sid in sids]
    frame["sessionPresence"] = True
    thread_hops.clear()
    response = await host.handle_message("conn-1", frame)
    assert response["rejected"] == []
    assert len(thread_hops) <= 24 * 6 + 8, len(thread_hops)


@pytest.mark.asyncio
async def test_a_full_events_frame_costs_a_bounded_number_of_thread_pool_calls(transport, thread_hops):
    """32 events is one frame's cap: they land in the one transaction, so the cost does not
    grow with them. The workspace hint costs 4 calls per member with a live stream."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    sid = await start(transport)
    body = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "b1",
        "afterSeq": 0,
        "events": [{"kind": "text", "role": "assistant", "text": f"line {i}"} for i in range(32)],
    }
    thread_hops.clear()
    response = await relay(transport).handle_message("conn-1", body)
    assert response["afterSeq"] == 32
    assert len(thread_hops) <= 12, len(thread_hops)


@pytest.mark.asyncio
async def test_a_heartbeat_reports_the_time_of_each_of_its_steps(transport):
    from core.services import local_session_relay as module

    sid = await start(transport)
    steps: dict[str, float] = {}
    token = module.frame_steps.set(steps)
    try:
        await relay(transport).handle_message("conn-1", {**heartbeat(sid), "sessionPresence": True})
    finally:
        module.frame_steps.reset(token)
    assert set(steps) == {"connect", "screens", "renew", "resume", "pickup", "watching", "presence"}


@pytest.mark.asyncio
async def test_a_session_hint_fans_out_off_the_callers_frame(monkeypatch):
    """51-member fan-out took 2.4-3.2 s of a desktop register frame on dev (2026-10-08); the caller
    must return at once and the publish still happen."""
    import asyncio

    from core.services import local_session_relay as relay

    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_publish(_self, owner, event):
        started.set()
        await asyncio.sleep(0.05)
        assert event["type"] == "workspace.sessions.changed" and event["payload"]["humanUserId"] == "user_1"
        finished.set()

    monkeypatch.setattr("core.services.live_events.LiveEventPublisher.publish_sessions", slow_publish)
    await asyncio.wait_for(relay.publish_resources_changed("org_1", "user_1"), timeout=0.01)  # returns at once
    assert not finished.is_set()
    await asyncio.wait_for(finished.wait(), timeout=1)  # and the hint still went out
    await asyncio.sleep(0)
    assert not relay._hint_tasks  # the finished task released itself


@pytest.mark.asyncio
async def test_hints_for_one_person_coalesce_while_a_fan_out_is_running(monkeypatch):
    """Codex on #3909: a streaming burst (one hint per session per second) must not pile up
    detached fan-outs; at most one runs per (owner, human, transcript) with one follow-up."""
    import asyncio

    from core.services import local_session_relay as relay

    calls, gate = [], asyncio.Event()

    async def publish(_self, owner, event):
        calls.append(event["payload"])
        await gate.wait()

    monkeypatch.setattr("core.services.live_events.LiveEventPublisher.publish_sessions", publish)
    for _ in range(5):
        await relay.publish_resources_changed("org_1", "user_1")
    await asyncio.sleep(0)
    assert len(calls) == 1 and len(relay._hint_tasks) == 1  # one in flight, the other four coalesced
    gate.set()
    await asyncio.gather(*list(relay._hint_tasks))
    assert len(calls) == 2  # exactly one follow-up carried the coalesced hints
    assert "org_1" not in {k[0] for k in relay._hint_running}
    # Another session's hint is its own fan-out: coalescing it would drop which row changed.
    calls.clear()
    gate.clear()
    await relay.publish_resources_changed("org_1", "user_1", session_id="s1")
    await relay.publish_resources_changed("org_1", "user_1", session_id="s2")
    await asyncio.sleep(0)
    assert [c.get("sessionId") for c in calls] == ["s1", "s2"]
    gate.set()
    await asyncio.gather(*list(relay._hint_tasks))


@pytest.mark.asyncio
async def test_an_open_session_browser_shows_until_its_lease_and_survives_reregistration(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET browserOpen = :b, browserPlace = :p",
        ExpressionAttributeValues={":b": {"where": "mac", "until": int(time.time()) + 60}, ":p": "cloud"},
    )
    row = await repo.get_session(OWNER, sid)
    assert service._session_public(row, ALICE.user_id).browser == "mac"
    await relay(transport).handle_message("conn-1", registration())
    row = await repo.get_session(OWNER, sid)
    assert row["browserOpen"]["where"] == "mac" and row["browserPlace"] == "cloud"
    assert service.browser_open(row, now=int(time.time()) + 61) is None


def test_waiting_copy_names_the_prompt_and_never_an_id():
    from core.services.local_session_relay import attention_copy

    row = {"provider": "claude", "sessionId": "0123456789abcdef", "title": "Fix login", "displayTitle": "Login bug"}
    permission = {"tool": "Bash", "summary": "npm test", "exact": True}
    assert attention_copy({**row, "pendingApproval": permission}, "waiting") == (
        "Claude · Login bug",
        'Bash wants to run "npm test"',
    )
    # Not answerable from Lightbulb (cut or redacted): say where it is answered.
    assert attention_copy({**row, "pendingApproval": {**permission, "exact": False}}, "waiting")[1] == (
        'Bash wants to run "npm test": answer it in your terminal'
    )
    question = {"tool": "AskUserQuestion", "summary": "Tea or coffee? (Tea / Coffee)", "exact": True}
    assert attention_copy({**row, "pendingApproval": question}, "waiting")[1] == (
        "Has a question: Tea or coffee? (Tea / Coffee)"
    )
    # No title: the folder, then "New chat" -- never the id.
    untitled = {"provider": "codex", "sessionId": "0123456789abcdef"}
    assert attention_copy({**untitled, "cwdName": "lightbulb"}, "waiting")[0] == "Codex · lightbulb"
    assert attention_copy(untitled, "waiting") == (
        "Codex · New chat",
        "Waiting for you: approve or answer it in your terminal",
    )


@pytest.mark.asyncio
async def test_waiting_on_a_question_names_it_from_the_prompt_row(transport):
    # A question is never held, so it has no pendingApproval: its transcript row names it.
    from core.services import local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **_):
        pushes.append(body)

    with patch("time.time", return_value=10000):
        sid = await start(transport)
    question = {"kind": "lifecycle", "role": "system", "text": "Tea or coffee? (Tea / Coffee)"}
    question["lifecycle"] = "approval_pending"
    batch = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "q1",
        "afterSeq": 0,
        "events": [question],
    }
    with patch("core.services.push_service.send_push_to_user", push):
        for stamp, message in ((10010, batch), (10011, heartbeat(sid, status="waiting-approval"))):
            with patch("time.time", return_value=stamp):
                await relay(transport).handle_message("conn-1", message)
                await asyncio.gather(*module._tasks)
    assert pushes == ["Has a question: Tea or coffee? (Tea / Coffee)"]
    # An unheld permission prompt keeps the desktop's wording and says where to answer it.
    assert module.waiting_line(None, "Wants to run Bash(git push)") == (
        "Wants to run Bash(git push): answer it in your terminal"
    )


@pytest.mark.asyncio
async def test_a_new_wait_never_names_the_previous_prompt(transport):
    from core.services import local_session_relay as module

    pushes = []

    async def push(user_id, title, body, data=None, **_):
        pushes.append(body)

    with patch("time.time", return_value=10000):
        sid = await start(transport)

    def prompt(text):
        return {"kind": "lifecycle", "role": "system", "text": text, "lifecycle": "approval_pending"}

    def batch(batch_id, after, events):
        return {
            "type": "local.session.events",
            "sessionId": sid,
            "generation": "generation-1",
            "batchId": batch_id,
            "afterSeq": after,
            "events": events,
        }

    answered = {"kind": "text", "role": "assistant", "text": "Tea it is."}
    frames = [
        (10010, batch("a", 0, [prompt("A? (Yes / No)")])),
        (10011, heartbeat(sid, status="waiting-approval")),
        (10012, heartbeat(sid, status="busy")),
        (10013, batch("a2", 1, [answered])),  # prompt A is answered
        # A new wait 10 s later: the heartbeat lands before prompt B's batch.
        (10020, heartbeat(sid, status="waiting-approval")),
        (10021, batch("b", 2, [prompt("B? (Tea / Coffee)")])),
    ]
    with patch("core.services.push_service.send_push_to_user", push):
        for stamp, message in frames:
            with patch("time.time", return_value=stamp):
                await relay(transport).handle_message("conn-1", message)
                await asyncio.gather(*module._tasks)
    # Never A for the second wait: the generic line, once (one wait is one notification).
    assert pushes == [
        "Has a question: A? (Yes / No)",
        "Waiting for you: approve or answer it in your terminal",
    ]
    # Once B's row is stored, the same wait reads as B.
    row = await module.repo.get_session(OWNER, sid)
    assert module.waiting_line(None, await module._waiting_text(row)) == "Has a question: B? (Tea / Coffee)"


@pytest.mark.asyncio
async def test_a_new_process_on_the_same_live_conversation_continues_its_row(transport):
    """Claude moves one conversation across pids (a daemon respawn, the agent view's restart, a
    `--resume` in another pid). The Mac re-points the live row and names the generation we hold: the
    same session id goes on, as one row, keeping its "Only you", title and place in the lists."""
    with patch("core.services.local_session_relay.capture_product_event") as capture:
        sid = await start(transport)
        await relay(transport).handle_message("conn-1", registration(title="Ship row life"))
        await service.set_sharing(ALICE, sid, False)
        messaging_repo._table().update_item(
            Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
            UpdateExpression="SET removedAt = :at, displayTitle = :name",
            ExpressionAttributeValues={":at": 1, ":name": "Row life"},
        )
        moved = {**registration(generation="generation-2", title=None), "previousGeneration": "generation-1"}
        response = await relay(transport).handle_message("conn-1", moved)
    assert response["sessionId"] == sid
    row = await repo.get_session(OWNER, sid)
    assert row["generation"] == "generation-2" and "endedAt" not in row
    assert (row["title"], row["displayTitle"], row["unshared"], row["removedAt"]) == (
        "Ship row life",
        "Row life",
        True,
        1,
    )
    assert "local_session_shared" not in [c.kwargs["event"] for c in capture.call_args_list[1:]], "not a new share"
    # A generation that names none we hold is still a new run.
    await relay(transport).handle_message(
        "conn-1", {**fixture("session-ended"), "sessionId": sid, "generation": "generation-2", "endedAt": 10005}
    )
    await relay(transport).handle_message(
        "conn-1", {**registration(generation="generation-3", title=None), "previousGeneration": "generation-2"}
    )
    row = await repo.get_session(OWNER, sid)
    assert row.get("title") is None and "removedAt" not in row and row["displayTitle"] == "Row life"
