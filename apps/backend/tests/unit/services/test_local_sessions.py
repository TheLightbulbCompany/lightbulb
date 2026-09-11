"""Room-local resources: real DynamoDB conditions under Moto, no AWS or harness."""

import asyncio
import time
from unittest.mock import patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from pydantic import ValidationError

from core.auth import AuthContext, get_current_user
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service
from core.services.messaging import ApiProblem

OWNER = "org_test"
ALICE = AuthContext(user_id="user_alice", org_id=OWNER)
BOB = AuthContext(user_id="user_bob", org_id=OWNER)
OUTSIDER = AuthContext(user_id="user_outsider", org_id=OWNER, org_role="org:admin")
ROOM = "11111111-1111-4111-8111-111111111111"
OTHER_ROOM = "22222222-2222-4222-8222-222222222222"
MAC = "a" * 64
CLI = "b" * 64


@pytest.fixture
def db():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        containers = resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        containers.put_item(
            Item={
                "owner_id": OWNER,
                "paired_devices": {
                    MAC: {"userId": ALICE.user_id, "clientId": "openclaw-macos", "publicKey": "private-to-server"},
                    CLI: {"userId": ALICE.user_id, "clientId": "node-host", "publicKey": "also-private"},
                    "c" * 64: {"userId": BOB.user_id, "clientId": "node-host", "publicKey": "bob-key"},
                },
            }
        )
        for room in (ROOM, OTHER_ROOM):
            table.put_item(Item={"pk": f"ROOM#{room}", "sk": "META", "ownerId": OWNER, "roomId": room})
            for user in (ALICE.user_id, BOB.user_id):
                table.put_item(Item={"pk": f"ROOM#{room}", "sk": f"MEMBER#human#{user}", "kind": "human", "id": user})
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch("core.repositories.container_repo._get_table", return_value=containers),
            patch.object(messaging_repo.settings, "MESSAGING_TABLE", table.name),
        ):
            yield table, containers


async def registered(**overrides):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    descriptor = dict(
        provider="codex",
        profileFingerprint="f" * 64,
        nativeConversationId="thread-1",
        generation="generation-1",
        providerVersion="1.0",
        status="idle",
        capabilities={"sendMode": "queue", "steer": False, "interrupt": False},
        capabilitiesVerified=True,
        onlineUntil=int(time.time()) + 60,
    )
    descriptor.update(overrides)
    row = await service.register_session(OWNER, ALICE.user_id, MAC, service.SessionRegistration(**descriptor))
    return row["sessionId"]


def command(service, **changes):
    body = dict(requestId="request-1", expectedGeneration="generation-1", kind="send", text="continue")
    body.update(changes)
    return service.LocalSessionCommand(**body)


@pytest.mark.asyncio
async def test_shared_resources_and_canonical_membership_on_every_operation(db):
    sid = await registered()
    resources = await service.list_resources(BOB, ROOM)
    assert resources.model_dump(by_alias=True)["devices"] == [
        {"deviceId": MAC, "humanUserId": ALICE.user_id, "cliDeviceId": CLI}
    ]
    assert [s.session_id for s in resources.sessions] == [sid]
    assert "publicKey" not in resources.model_dump_json() and "private-to-server" not in resources.model_dump_json()
    receipt = await service.submit_command(BOB, ROOM, sid, command(service))
    assert receipt.actor_user_id == BOB.user_id and receipt.room_id == ROOM and receipt.state == "pending"
    operations = [
        lambda auth, room: service.list_resources(auth, room),
        lambda auth, room: service.list_events(auth, room, sid, 0, 10),
        lambda auth, room: service.submit_command(auth, room, sid, command(service)),
        lambda auth, room: service.get_command(auth, room, sid, "request-1"),
    ]
    for auth in (OUTSIDER, AuthContext(user_id=BOB.user_id, org_id="org_other")):
        for operation in operations:
            with pytest.raises(ApiProblem):
                await operation(auth, ROOM)
    # Both directory and canonical session remain; membership deletion alone revokes access.
    db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{ALICE.user_id}"})
    assert (await service.list_resources(BOB, ROOM)).sessions == []
    for operation in operations[1:]:
        with pytest.raises(ApiProblem):
            await operation(BOB, ROOM)
    assert (await service.list_resources(BOB, OTHER_ROOM)).sessions[0].session_id == sid
    assert await repo.get_session(OWNER, sid) is not None
    db[0].delete_item(Key={"pk": f"ROOM#{OTHER_ROOM}", "sk": f"MEMBER#human#{BOB.user_id}"})
    for operation in operations:
        with pytest.raises(ApiProblem):
            await operation(BOB, OTHER_ROOM)


@pytest.mark.asyncio
async def test_enrollment_ownership_disable_and_no_machine_self_enable(db):
    with pytest.raises(ApiProblem):
        await service.set_enrollment(BOB, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId="c" * 64, enabled=True))
    with pytest.raises(ApiProblem):
        await service.register_session(
            OWNER,
            ALICE.user_id,
            MAC,
            service.SessionRegistration(
                provider="codex",
                profileFingerprint="f" * 64,
                nativeConversationId="thread-1",
                generation="generation-1",
                providerVersion="1.0",
            ),
        )
    sid = await registered()
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=False))
    assert (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True) == {"devices": [], "sessions": []}
    with pytest.raises(ApiProblem):
        await service.list_events(BOB, ROOM, sid, 0, 10)
    assert db[0].get_item(Key={"pk": f"LOCALSESSION#{OWNER}#{sid}", "sk": "META"}).get("Item")


@pytest.mark.asyncio
async def test_reconnect_identity_generation_and_projection_are_consistent(db):
    first = await registered()
    assert await registered(generation="generation-2") == first
    other = await registered(profileFingerprint="e" * 64)
    assert other != first
    row = await repo.get_session(OWNER, first)
    assert row["generation"] == "generation-2"
    projections = await repo.list_user_sessions(OWNER, ALICE.user_id)
    assert {r["sessionId"] for r in projections} == {first, other}
    assert len([r for r in db[0].scan()["Items"] if r["sk"] == "META" and r["pk"].startswith("LOCALSESSION#")]) == 2
    assert all("expiresAt" not in r for r in db[0].scan()["Items"])


@pytest.mark.asyncio
async def test_command_idempotency_ordering_and_receipt_provenance(db):
    sid = await registered()
    first = await service.submit_command(BOB, ROOM, sid, command(service))
    assert (await service.submit_command(BOB, ROOM, sid, command(service))) == first
    assert (await repo.get_session(OWNER, sid))["commandSeq"] == 1
    for actor, room, changes in ((BOB, ROOM, {"text": "changed"}), (ALICE, ROOM, {}), (BOB, OTHER_ROOM, {})):
        with pytest.raises(ApiProblem, match="request"):
            await service.submit_command(actor, room, sid, command(service, **changes))
    receipts = await asyncio.gather(
        *[service.submit_command(BOB, ROOM, sid, command(service, requestId=f"parallel-{i}")) for i in range(2)]
    )
    assert sorted(r.command_seq for r in receipts) == [2, 3]
    assert 0 < first.deliver_by - int(time.time()) <= 30
    assert (await service.get_command(BOB, ROOM, sid, "request-1")) == first
    # A different eligible room must not reveal command provenance from this room.
    with pytest.raises(ApiProblem):
        await service.get_command(BOB, OTHER_ROOM, sid, "request-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "descriptor,changes",
    [
        ({}, {"expectedGeneration": "old"}),
        ({}, {"kind": "interrupt", "text": None}),
        ({"status": "busy", "activeTurnId": "turn-1"}, {"expectedTurnId": "turn-1"}),
        (
            {"status": "busy", "activeTurnId": "turn-2", "capabilities": {"sendMode": "queue", "steer": True}},
            {"expectedTurnId": "turn-1"},
        ),
        ({"status": "offline"}, {}),
        ({"onlineUntil": 1}, {}),
        ({"capabilitiesVerified": False, "providerVersion": "2.0"}, {}),
    ],
)
async def test_commands_fail_closed_for_generation_capability_turn_and_offline(db, descriptor, changes):
    sid = await registered(**descriptor)
    with pytest.raises(ApiProblem):
        await service.submit_command(BOB, ROOM, sid, command(service, **changes))
    assert await repo.get_command(OWNER, sid, "request-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["requester", "owner", "room-scope", "enrollment", "pairing", "generation"])
async def test_admission_transaction_rejects_revocation_after_authorization(db, mutation):
    sid = await registered()
    real_transact = messaging_repo._transact
    fired = False

    async def race(items):
        nonlocal fired
        if not fired:
            fired = True
            if mutation in ("requester", "owner"):
                user = BOB.user_id if mutation == "requester" else ALICE.user_id
                db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{user}"})
            elif mutation == "pairing":
                db[1].update_item(
                    Key={"owner_id": OWNER},
                    UpdateExpression="REMOVE paired_devices.#d",
                    ExpressionAttributeNames={"#d": MAC},
                )
            else:
                pk, sk, field, value = {
                    "room-scope": (f"ROOM#{ROOM}", "META", "ownerId", "org_other"),
                    "enrollment": (f"LOCALUSER#{OWNER}#{ALICE.user_id}", f"DEVICE#{MAC}", "enabled", False),
                    "generation": (f"LOCALSESSION#{OWNER}#{sid}", "META", "generation", "generation-2"),
                }[mutation]
                db[0].update_item(
                    Key={"pk": pk, "sk": sk},
                    UpdateExpression="SET #f = :v",
                    ExpressionAttributeNames={"#f": field},
                    ExpressionAttributeValues={":v": value},
                )
        await real_transact(items)

    with patch.object(messaging_repo, "_transact", race), pytest.raises(ApiProblem):
        await service.submit_command(BOB, ROOM, sid, command(service))
    assert await repo.get_command(OWNER, sid, "request-1") is None
    assert (await repo.get_session(OWNER, sid))["commandSeq"] == 0


@pytest.mark.asyncio
async def test_events_are_paginated_and_internal_fields_are_not_exposed(db):
    sid = await registered()
    for seq in range(1, 4):
        db[0].put_item(
            Item={
                "pk": f"LOCALSESSION#{OWNER}#{sid}",
                "sk": f"EVENT#{seq:020d}",
                "seq": seq,
                "kind": "text",
                "text": f"message {seq}",
                "role": "assistant",
                "truncated": False,
            }
        )
    page = await service.list_events(BOB, ROOM, sid, 0, 2)
    assert [e.text for e in page.events] == ["message 1", "message 2"] and page.next_after_seq == 2
    page = await service.list_events(BOB, ROOM, sid, 2, 2)
    assert [e.seq for e in page.events] == [3] and page.next_after_seq is None
    assert "LOCALSESSION" not in page.model_dump_json()
    with pytest.raises(ApiProblem):
        await service.list_events(BOB, ROOM, sid, -1, 101)


@pytest.mark.parametrize(
    "extra",
    [
        {"actorUserId": "user_alice"},
        {"deviceId": MAC},
        {"ownerId": OWNER},
        {"kind": "shell"},
        {"text": "😀" * 5000},
        {"requestId": "bad#id"},
        {"text": "  "},
    ],
)
def test_command_trust_boundary_rejects_forgery_and_unbounded_input(extra):
    with pytest.raises(ValidationError):
        command(service, **extra)


@pytest.mark.asyncio
async def test_http_routes_use_clerk_identity_and_validate_payloads(db):
    sid = await registered()
    from routers import desktop_pair, messaging

    app = FastAPI()
    app.include_router(desktop_pair.router, prefix="/api/v1")
    app.include_router(messaging.router, prefix="/api/v1")
    app.add_exception_handler(
        ApiProblem, lambda request, exc: JSONResponse(status_code=exc.status_code, content=exc.body())
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        path = "/api/v1/desktop/local-sessions/enrollment"
        assert (await client.put(path, json={"deviceId": MAC, "enabled": True})).status_code in (401, 403)
        app.dependency_overrides[get_current_user] = lambda: BOB
        assert (await client.put(path, json={"deviceId": MAC, "enabled": True})).status_code == 403
        base = f"/api/v1/messaging/rooms/{ROOM}"
        assert (await client.get(f"{base}/resources")).status_code == 200
        assert (await client.get(f"{base}/local-sessions/{sid}/events?afterSeq=0&limit=2")).status_code == 200
        payload = command(service).model_dump(by_alias=True)
        assert (
            await client.post(f"{base}/local-sessions/{sid}/commands", json={**payload, "actorUserId": ALICE.user_id})
        ).status_code == 422
        response = await client.post(f"{base}/local-sessions/{sid}/commands", json=payload)
        assert response.status_code == 202 and response.json()["actorUserId"] == BOB.user_id
        assert (await client.get(f"{base}/local-sessions/{sid}/commands/request-1")).json() == response.json()
        app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=ALICE.user_id)
        assert (await client.put(path, json={"deviceId": MAC, "enabled": True})).status_code == 409


@pytest.mark.asyncio
async def test_maximum_valid_command_returns_receipt_after_admission(db):
    sid = await registered()
    request = command(service, text="x" * 8000)
    receipt = await service.submit_command(BOB, ROOM, sid, request)
    assert receipt.text == "x" * 8000
    assert (await repo.get_command(OWNER, sid, "request-1"))["text"] == receipt.text


def test_command_reserves_space_for_server_framing_inside_wire_envelope():
    # Both requests fit 16 KiB but exceed the reserved 8 KiB command budget.
    for text in ("x" * 8100, "é" * 4100):
        with pytest.raises(ValidationError):
            command(service, text=text)


@pytest.mark.asyncio
async def test_registration_transaction_keeps_canonical_and_projection_atomic(db):
    sid = await registered()
    before = await repo.get_session(OWNER, sid)
    real_transact = messaging_repo._transact

    async def revoke_then_write(items):
        db[1].update_item(
            Key={"owner_id": OWNER}, UpdateExpression="REMOVE paired_devices.#d", ExpressionAttributeNames={"#d": MAC}
        )
        await real_transact(items)

    with patch.object(messaging_repo, "_transact", revoke_then_write), pytest.raises(ApiProblem):
        await service.register_session(
            OWNER,
            ALICE.user_id,
            MAC,
            service.SessionRegistration(
                provider="codex",
                profileFingerprint="f" * 64,
                nativeConversationId="thread-1",
                generation="generation-2",
                providerVersion="2.0",
            ),
        )
    assert await repo.get_session(OWNER, sid) == before
    assert len(await repo.list_user_sessions(OWNER, ALICE.user_id)) == 1


@pytest.mark.asyncio
async def test_provider_upgrade_requires_new_compatibility_verification_and_preserves_order(db):
    sid = await registered()
    await service.submit_command(ALICE, ROOM, sid, command(service))
    upgraded = await service.register_session(
        OWNER,
        ALICE.user_id,
        MAC,
        service.SessionRegistration(
            provider="codex",
            profileFingerprint="f" * 64,
            nativeConversationId="thread-1",
            generation="generation-2",
            providerVersion="2.0",
            status="idle",
            onlineUntil=int(time.time()) + 60,
            capabilities={"sendMode": "queue"},
        ),
    )
    assert upgraded["sessionId"] == sid and upgraded["commandSeq"] == 1
    public = (await service.list_resources(BOB, ROOM)).sessions[0]
    assert public.capabilities.send_mode == "none" and public.capabilities_verified is False
    with pytest.raises(ApiProblem):
        await service.submit_command(
            BOB, ROOM, sid, command(service, requestId="request-2", expectedGeneration="generation-2")
        )
    assert (await repo.get_command(OWNER, sid, "request-1"))["state"] == "pending"


def test_normalized_event_envelope_is_utf8_bounded_and_forbids_raw_payloads():
    for extras in ({"text": "😀" * 5000}, {"raw": {"config": "secret"}}):
        with pytest.raises(ValidationError):
            service.LocalSessionEvent(**{"seq": 1, "kind": "text", "role": "assistant", "text": "hello", **extras})


@pytest.mark.asyncio
async def test_machine_device_proof_cannot_enable_human_enrollment(db):
    import jwt
    from unittest.mock import AsyncMock
    from routers.desktop_pair import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    machine_token = jwt.encode(
        {"sub": ALICE.user_id, "o": {"id": OWNER}, "deviceId": MAC},
        "machine-key",
        algorithm="HS256",
        headers={"kid": MAC},
    )
    with patch("core.auth._get_cached_jwks", AsyncMock(return_value={"keys": []})):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/v1/desktop/local-sessions/enrollment",
                headers={"Authorization": f"Bearer {machine_token}"},
                json={"deviceId": MAC, "enabled": True},
            )
    assert response.status_code == 401
    assert db[0].get_item(Key={"pk": f"LOCALUSER#{OWNER}#{ALICE.user_id}", "sk": f"DEVICE#{MAC}"}).get("Item") is None
