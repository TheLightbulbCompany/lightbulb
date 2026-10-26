"""Room-local resources: real DynamoDB conditions under Moto, no AWS or harness."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from pydantic import ValidationError

from core.auth import AuthContext, get_current_user, get_human_session
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
                table.put_item(
                    Item={
                        "pk": f"ROOM#{room}",
                        "sk": f"MEMBER#human#{user}",
                        "kind": "human",
                        "id": user,
                        "ownerId": OWNER,
                    }
                )
        users = resource.create_table(
            TableName="test-users",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch("core.repositories.container_repo._get_table", return_value=containers),
            patch("core.repositories.user_repo._get_table", return_value=users),
            patch.object(messaging_repo.settings, "MESSAGING_TABLE", table.name),
        ):
            yield table, containers


async def registered(**overrides):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    descriptor = dict(
        provider="codex",
        profileFingerprint="f" * 64,
        nativeConversationId=overrides.pop("conversation", "thread-1"),
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
async def test_lightbulb_display_title_survives_native_registration(db, monkeypatch):
    from core.services import local_session_relay

    monkeypatch.setattr(local_session_relay, "publish_resources_changed", AsyncMock())
    sid = await registered(title="Native title")
    renamed = await service.set_display_title(ALICE, sid, "Chosen title")
    assert renamed.display_title == "Chosen title" and renamed.title == "Native title"
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
            status="idle",
            capabilities={"sendMode": "queue", "steer": False, "interrupt": False},
            capabilitiesVerified=True,
            onlineUntil=int(time.time()) + 60,
            title="Updated native title",
        ),
    )
    current = await service.get_session(ALICE, sid)
    assert current.title == "Updated native title" and current.display_title == "Chosen title"


@pytest.mark.asyncio
async def test_agent_invocation_is_room_bound_typed_and_revocable(db, monkeypatch):
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent})
    token = grants.mint(OWNER, ROOM, agent, "44444444-4444-4444-8444-444444444444", ALICE.user_id)
    caller = grants.verify(token)
    sid = await registered()
    assert (await service.list_resources(caller, ROOM)).sessions[0].session_id == sid
    with pytest.raises(ApiProblem):
        await service.list_resources(caller, OTHER_ROOM)
    # An agent reads the session but never commands it, whoever woke it: it messages through the engine.
    from dataclasses import replace as _replace

    for requester in (ALICE.user_id, BOB.user_id, None):
        other = _replace(caller, requester_id=requester)
        assert (await service.list_events(other, ROOM, sid)).events == []
        with pytest.raises(ApiProblem, match="Only a person"):
            await service.submit_command(other, ROOM, sid, command(service))
    from dataclasses import replace

    with pytest.raises(ApiProblem):
        await service.list_resources(replace(caller, expires_at=int(time.time())), ROOM)


def test_invocation_grant_rejects_wrong_purpose_types_scope_and_lifetime(monkeypatch):
    import jwt
    from core.services import local_session_grants as grants

    key = "hermetic-signing-key"
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", key)
    token = grants.mint(OWNER, ROOM, "33333333-3333-4333-8333-333333333333", OTHER_ROOM)
    original = jwt.decode(token, options={"verify_signature": False})
    from core.services import service_token

    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)
    with pytest.raises(ApiProblem):
        grants.verify(service_token.mint(OWNER))
    for changes in (
        {"aud": "paperclip"},
        {"kind": "paperclip_service"},
        {"exp": int(time.time()) - 1},
        {"exp": original["iat"] + 1801},
        {"iat": True},
        {"ownerId": 2},
        {"roomId": OTHER_ROOM},
        {"sessionKey": "agent:other:room"},
    ):
        bad = jwt.encode({**original, **changes}, key, algorithm="HS256")
        with pytest.raises(ApiProblem) as rejected:
            grants.verify(bad)
        assert bad not in str(rejected.value)


@pytest.mark.asyncio
async def test_agent_router_derives_room_only_from_verified_bearer(db, monkeypatch):
    from core.services import local_session_grants as grants
    from routers import messaging as routes
    from starlette.requests import Request

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent})
    token = grants.mint(OWNER, ROOM, agent, OTHER_ROOM)
    request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    caller = await routes.local_session_invocation(request)
    assert caller.room_id == ROOM
    assert await routes.agent_local_session_resources(caller) == await service.list_resources(caller, ROOM)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer owner-hook-token")]})
    with pytest.raises(ApiProblem):
        await routes.local_session_invocation(request)


def test_workspace_public_summary_is_bounded_and_never_accepts_local_paths():
    summary = service.WorkspaceSummary(revision=2, coverage="bounded", changed=True, overlap=True)
    assert summary.model_dump(by_alias=True) == {"revision": 2, "coverage": "bounded", "changed": True, "overlap": True}
    with pytest.raises(ValidationError):
        service.WorkspaceSummary(revision=2, coverage="bounded", changed=True, overlap=True, root="/private/path")


@pytest.mark.asyncio
async def test_late_conditional_disable_cannot_overwrite_new_enrollment(db):
    with pytest.raises(ValidationError):
        service.EnrollmentRequest(deviceId=MAC, enabled=True, expectedRevision="old")
    first = await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    disable = service.EnrollmentRequest(deviceId=MAC, enabled=False, expectedRevision=first.revision)
    # Old cleanup can commit before the next enable; its later retry cannot.
    await service.set_enrollment(ALICE, disable)
    newest = await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    with pytest.raises(ApiProblem) as rejected:
        await service.set_enrollment(ALICE, disable)
    assert rejected.value.error.code == "LOCAL_SESSION_CONFLICT"
    retained = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    assert retained["enabled"] and retained["revision"] == newest.revision
    # Also fence an old request that never committed at all before re-enable.
    old = service.EnrollmentRequest(deviceId=MAC, enabled=False, expectedRevision=newest.revision)
    final = await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, old)
    assert (await repo.get_enrollment(OWNER, ALICE.user_id, MAC))["revision"] == final.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("predecessor_exists", [False, True])
async def test_nominated_enrollment_cancellation_fences_first_and_duplicate_enable(db, predecessor_exists):
    previous = None
    if predecessor_exists:
        first = await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
        previous = first.revision
        await service.set_enrollment(
            ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=False, expectedRevision=previous)
        )
    attempt = "aaaaaaaa-1111-4111-8111-111111111111"
    enable = service.EnrollmentRequest(
        deviceId=MAC, ownerId=OWNER, enabled=True, enableRevision=attempt, previousRevision=previous
    )
    cancel = service.EnrollmentRequest(
        deviceId=MAC, ownerId=OWNER, enabled=False, expectedRevision=attempt, previousRevision=previous
    )
    # The HTTP enable has not reached DynamoDB yet. Cancellation must still fence it.
    stopped = await service.set_enrollment(ALICE, cancel)
    assert stopped.revision == attempt and not stopped.enabled
    assert stopped.owner_id == OWNER
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, enable)
    assert not (await service.set_enrollment(ALICE, cancel)).enabled
    newer = "bbbbbbbb-2222-4222-8222-222222222222"
    await service.set_enrollment(
        ALICE,
        service.EnrollmentRequest(
            deviceId=MAC, ownerId=OWNER, enabled=True, enableRevision=newer, previousRevision=attempt
        ),
    )
    for obsolete in (enable, cancel):
        with pytest.raises(ApiProblem):
            await service.set_enrollment(ALICE, obsolete)
    assert (await repo.get_enrollment(OWNER, ALICE.user_id, MAC))["revision"] == newer


@pytest.mark.asyncio
async def test_nominated_enrollment_lost_ack_recovery_revokes_retained_history(db):
    sid = await registered()
    previous = (await repo.get_enrollment(OWNER, ALICE.user_id, MAC))["revision"]
    await service.set_enrollment(
        ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=False, expectedRevision=previous)
    )
    attempt = "cccccccc-3333-4333-8333-333333333333"
    enable = service.EnrollmentRequest(
        deviceId=MAC, ownerId=OWNER, enabled=True, enableRevision=attempt, previousRevision=previous
    )
    await service.set_enrollment(ALICE, enable)  # response lost / subsequent local write fails
    assert (await service.list_events(BOB, ROOM, sid)).events == []
    cancel = service.EnrollmentRequest(
        deviceId=MAC, ownerId=OWNER, enabled=False, expectedRevision=attempt, previousRevision=previous
    )
    for _ in range(2):
        assert not (await service.set_enrollment(ALICE, cancel)).enabled
    with pytest.raises(ApiProblem):
        await service.list_events(BOB, ROOM, sid)
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, enable)
    assert await repo.get_session(OWNER, sid)  # retained, not deleted


@pytest.mark.asyncio
async def test_nominated_absent_scope_never_overwrites_an_existing_target_revision(db):
    existing = await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=True))
    await service.set_enrollment(
        ALICE, service.EnrollmentRequest(deviceId=MAC, enabled=False, expectedRevision=existing.revision)
    )
    attempt = "dddddddd-4444-4444-8444-444444444444"
    for request in (
        service.EnrollmentRequest(
            deviceId=MAC, ownerId=OWNER, enabled=True, enableRevision=attempt, previousRevision=None
        ),
        service.EnrollmentRequest(
            deviceId=MAC, ownerId=OWNER, enabled=False, expectedRevision=attempt, previousRevision=None
        ),
    ):
        with pytest.raises(ApiProblem):
            await service.set_enrollment(ALICE, request)
    row = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    assert row["revision"] == existing.revision and not row["enabled"]


@pytest.mark.asyncio
async def test_nominated_enrollment_rejects_wrong_owner_and_ambiguous_predecessor(db):
    with pytest.raises(ApiProblem):
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, ownerId="other", enabled=True))
    assert await repo.get_enrollment(OWNER, ALICE.user_id, MAC) is None
    for extra in (
        {"enableRevision": "aaaaaaaa-1111-4111-8111-111111111111"},
        {"previousRevision": None},
        {"enableRevision": "bad", "previousRevision": None, "ownerId": OWNER},
        {
            "enableRevision": "aaaaaaaa-1111-4111-8111-111111111111",
            "previousRevision": "aaaaaaaa-1111-4111-8111-111111111111",
            "ownerId": OWNER,
        },
    ):
        with pytest.raises(ValidationError):
            service.EnrollmentRequest(deviceId=MAC, enabled=True, **extra)
    with pytest.raises(ValidationError):
        service.EnrollmentRequest(
            deviceId=MAC,
            enabled=False,
            ownerId=OWNER,
            expectedRevision="aaaaaaaa-1111-4111-8111-111111111111",
            previousRevision="aaaaaaaa-1111-4111-8111-111111111111",
        )
    with pytest.raises(ValidationError):
        service.EnrollmentRequest(
            deviceId=MAC, enabled=False, ownerId=OWNER, expectedRevision="malformed", previousRevision=None
        )


@pytest.mark.asyncio
async def test_equivalent_uuid_spellings_share_resources_events_and_one_command_receipt(db):
    sid = await registered()
    room = "aabbccdd-1111-4111-8111-aabbccddeeff"
    table = db[0]
    table.put_item(Item={"pk": f"ROOM#{room}", "sk": "META", "ownerId": OWNER, "roomId": room})
    for user in (ALICE.user_id, BOB.user_id):
        table.put_item(
            Item={"pk": f"ROOM#{room}", "sk": f"MEMBER#human#{user}", "kind": "human", "id": user, "ownerId": OWNER}
        )
    table.put_item(
        Item={
            "pk": f"LOCALSESSION#{OWNER}#{sid}",
            "sk": "EVENT#00000000000000000001",
            "seq": 1,
            "kind": "text",
            "text": "canonical history",
            "role": "assistant",
            "truncated": False,
        }
    )
    first = await service.submit_command(BOB, room, sid, command(service))
    resources = await service.list_resources(BOB, room)
    events = await service.list_events(BOB, room, sid)
    assert [session.session_id for session in resources.sessions] == [sid]
    assert [event.text for event in events.events] == ["canonical history"]
    for room_spelling, session_spelling in ((room, sid.upper()), (room.upper(), sid), (room.upper(), sid.upper())):
        assert await service.submit_command(BOB, room_spelling, session_spelling, command(service)) == first
        assert await service.get_command(BOB, room_spelling, session_spelling, "request-1") == first
        assert await service.list_resources(BOB, room_spelling) == resources
        assert await service.list_events(BOB, room_spelling, session_spelling) == events
    assert await service.get_command(BOB, room, sid, "request-1") == first
    assert (await repo.get_session(OWNER, sid))["commandSeq"] == 1
    receipts = [row for row in table.scan()["Items"] if row["sk"].startswith("COMMAND#")]
    assert len(receipts) == 1
    assert receipts[0]["pk"] == f"LOCALSESSION#{OWNER}#{sid}"
    assert receipts[0]["roomId"] == room and receipts[0]["sessionId"] == sid


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
    disabled = await service.list_resources(BOB, ROOM)
    assert disabled.devices == [] and disabled.sessions == []
    with pytest.raises(ApiProblem):
        await service.list_events(BOB, ROOM, sid, 0, 10)
    assert db[0].get_item(Key={"pk": f"LOCALSESSION#{OWNER}#{sid}", "sk": "META"}).get("Item")


def _cancelled(reason: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": reason}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


@pytest.mark.asyncio
async def test_enrollment_retries_a_concurrent_pairing_conflict_but_not_a_failed_condition(db, monkeypatch):
    """Load 3b (dev 2026-10-08): a Mac pairing in the same workspace held the owner row, so the
    enrollment transaction was cancelled with TransactionConflict and misreported as "Device
    pairing changed". A conflict now retries; a real ConditionalCheckFailed still 403s at once."""
    real = messaging_repo._transact
    failures = [_cancelled("TransactionConflict")]

    async def flaky(items):
        if failures:
            raise failures.pop()
        return await real(items)

    monkeypatch.setattr(messaging_repo, "_transact", flaky)
    sleep = AsyncMock()
    with patch("asyncio.sleep", sleep):
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
        assert sleep.await_count == 1 and not failures

        failures.append(_cancelled("ConditionalCheckFailed"))
        with pytest.raises(ApiProblem) as refused:
            await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    assert (
        refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
        and str(refused.value) == "Device pairing changed. Try again."
    )
    assert sleep.await_count == 1


@pytest.mark.asyncio
async def test_paired_directory_is_independent_of_sharing_and_only_groups_explicit_associations(db):
    resources = (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)
    assert resources["devices"] == [] and resources["sessions"] == []
    assert resources["pairedNodes"] == [
        {"deviceId": MAC, "humanUserId": ALICE.user_id, "clientId": "openclaw-macos", "macDeviceId": MAC},
        {"deviceId": CLI, "humanUserId": ALICE.user_id, "clientId": "node-host", "macDeviceId": None},
        {"deviceId": "c" * 64, "humanUserId": BOB.user_id, "clientId": "node-host", "macDeviceId": None},
    ]
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=False))
    grouped = (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)
    assert grouped["pairedNodes"][1]["macDeviceId"] == MAC
    assert "publicKey" not in str(grouped) and "online" not in str(grouped)
    second_mac = "d" * 64
    db[1].update_item(
        Key={"owner_id": OWNER},
        UpdateExpression="SET paired_devices.#d = :pair",
        ExpressionAttributeNames={"#d": second_mac},
        ExpressionAttributeValues={":pair": {"userId": ALICE.user_id, "clientId": "openclaw-macos"}},
    )
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=second_mac, cliDeviceId=CLI, enabled=False))
    ambiguous = (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)
    assert next(n for n in ambiguous["pairedNodes"] if n["deviceId"] == CLI)["macDeviceId"] is None
    db[0].delete_item(Key={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#human#{ALICE.user_id}"})
    remaining = (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)
    assert [n["deviceId"] for n in remaining["pairedNodes"]] == ["c" * 64]
    for auth in (OUTSIDER, AuthContext(user_id=BOB.user_id, org_id="org_other")):
        with pytest.raises(ApiProblem):
            await service.list_resources(auth, ROOM)


@pytest.mark.asyncio
async def test_latest_event_cursor_uses_canonical_metadata_not_directory_projection(db):
    sid = await registered()
    assert (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)["sessions"][0]["latestEventSeq"] == 0
    db[0].update_item(
        Key={"pk": f"LOCALSESSION#{OWNER}#{sid}", "sk": "META"},
        UpdateExpression="SET eventSeq = :seq",
        ExpressionAttributeValues={":seq": 123},
    )
    assert (await service.list_resources(BOB, ROOM)).model_dump(by_alias=True)["sessions"][0]["latestEventSeq"] == 123


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
    original_admit = repo.admit_command
    both_read = asyncio.Event()
    transaction_lock = asyncio.Lock()
    attempts = []
    cancellations = []

    async def competing_admit(
        row, enrollment, receipt, *, allow_offline=False, extra=(), source_pairing=None, **kwargs
    ):
        attempts.append((row["commandSeq"], row["revision"], receipt["requestId"]))
        # Force stale application snapshots, but serialize only Moto's transaction
        # execution: Moto does not support concurrent writes from worker threads.
        if len(attempts) == 2:
            both_read.set()
        await both_read.wait()
        async with transaction_lock:
            try:
                await original_admit(
                    row,
                    enrollment,
                    receipt,
                    allow_offline=allow_offline,
                    extra=extra,
                    source_pairing=source_pairing,
                    **kwargs,
                )
            except ClientError as exc:
                cancellations.append((receipt["requestId"], exc.response))
                raise

    with patch.object(repo, "admit_command", competing_admit):
        receipts = await asyncio.wait_for(
            asyncio.gather(
                *[service.submit_command(BOB, ROOM, sid, command(service, requestId=f"parallel-{i}")) for i in range(2)]
            ),
            timeout=10,
        )
    assert len(attempts) == 3
    assert attempts[0][:2] == attempts[1][:2]
    assert [attempt[0] for attempt in attempts] == [1, 1, 2]
    assert attempts[2][1] != attempts[0][1]
    assert len(cancellations) == 1
    rejected_request, error = cancellations[0]
    assert error["Error"]["Code"] == "TransactionCanceledException"
    # The actual META update (before the receipt and audit puts), not an invented
    # exception, rejects the stale revision; all other authorization/receipt checks pass.
    codes = [reason["Code"] for reason in error["CancellationReasons"]]
    assert codes[-3:] == ["ConditionalCheckFailed", "None", "None"]
    assert all(code == "None" for code in codes[:-3])
    assert attempts[2][2] == rejected_request
    assert sorted(r.command_seq for r in receipts) == [2, 3]
    assert (await repo.get_session(OWNER, sid))["commandSeq"] == 3
    for receipt in receipts:
        assert receipt.actor_user_id == BOB.user_id and receipt.room_id == ROOM
        assert await service.get_command(BOB, ROOM, sid, receipt.request_id) == receipt
        assert await service.submit_command(BOB, ROOM, sid, command(service, requestId=receipt.request_id)) == receipt
    assert (await repo.get_session(OWNER, sid))["commandSeq"] == 3
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
        app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: BOB
        assert (await client.put(path, json={"deviceId": MAC, "enabled": True})).status_code == 403
        # Sessions are workspace-scoped (2026-09-24): a teammate reads, only the owner commands.
        base = f"/api/v1/messaging/sessions/{sid}"
        assert (await client.get("/api/v1/messaging/sessions")).status_code == 200
        assert (await client.get(f"{base}/events?afterSeq=0&limit=2")).status_code == 200
        payload = command(service).model_dump(by_alias=True)
        assert (await client.post(f"{base}/commands", json=payload)).status_code == 403
        app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: ALICE
        assert (await client.post(f"{base}/commands", json={**payload, "actorUserId": BOB.user_id})).status_code == 422
        response = await client.post(f"{base}/commands", json=payload)
        assert response.status_code == 202 and response.json()["actorUserId"] == ALICE.user_id
        assert response.json()["roomId"] == ""
        assert (await client.get(f"{base}/commands/request-1")).json() == response.json()
        app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: BOB
        assert (await client.get(f"{base}/commands/request-1")).status_code == 403
        app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: AuthContext(
            user_id=ALICE.user_id
        )
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


@pytest.mark.asyncio
async def test_ended_row_reads_ended_even_while_lease_is_live(db):
    with patch("time.time", return_value=10000):
        sid = await registered()
    row = await repo.get_session(OWNER, sid)
    await repo.put_session(
        {**row, "endedAt": 10005, "lastEventAt": 10005, "eventSeq": 1},
        row,
        await repo.get_enrollment(OWNER, ALICE.user_id, MAC),
    )
    with patch("time.time", return_value=10010):
        resources = await service.list_resources(BOB, ROOM, include_ended=True)
    [session] = [s for s in resources.sessions if s.session_id == sid]
    assert session.status == "ended"
    assert session.ended_at == "1970-01-01T02:46:45Z"
    assert session.last_event_at == "1970-01-01T02:46:45Z"


@pytest.mark.asyncio
async def test_command_to_ended_session_is_refused(db):
    with patch("time.time", return_value=10000):
        sid = await registered()
    row = await repo.get_session(OWNER, sid)
    await repo.put_session({**row, "endedAt": 10005}, row, await repo.get_enrollment(OWNER, ALICE.user_id, MAC))
    with patch("time.time", return_value=10010), pytest.raises(ApiProblem) as excinfo:
        await service.submit_command(BOB, ROOM, sid, command(service))
    assert excinfo.value.error.code == "LOCAL_SESSION_OFFLINE"
    assert "ended" in excinfo.value.error.message


@pytest.mark.asyncio
async def test_list_resources_sorts_by_activity_hides_ended_and_caps_recent(db):
    sids = []
    for stamp in (10000, 10100, 10200):
        with patch("time.time", return_value=stamp):
            sids.append(await registered(conversation=f"conv-{stamp}"))
    rows = [await repo.get_session(OWNER, s) for s in sids]
    # end the middle one, and 21 more ended rows to prove the cap. Each `registered()`
    # call re-enrolls (fresh revision), so fetch the enrollment fresh right before use.
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**rows[1], "endedAt": 10150, "lastEventAt": 10150, "eventSeq": 1}, rows[1], enrollment)
    for i in range(21):
        with patch("time.time", return_value=20000 + i):
            s = await registered(conversation=f"ended-{i}")
        r = await repo.get_session(OWNER, s)
        enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
        await repo.put_session({**r, "endedAt": 20000 + i, "lastEventAt": 20000 + i, "eventSeq": 1}, r, enrollment)
    with patch("time.time", return_value=30000):
        live = await service.list_resources(BOB, ROOM)
        with_ended = await service.list_resources(BOB, ROOM, include_ended=True)
    assert [s.session_id for s in live.sessions] == [sids[2], sids[0]]
    assert [s.status for s in live.sessions] == ["offline", "offline"]
    ended = [s for s in with_ended.sessions if s.status == "ended"]
    assert len(ended) == 20 and ended[0].last_event_at == "1970-01-01T05:33:40Z"
    assert with_ended.sessions[:2] == live.sessions


async def test_list_workspace_sessions_is_owner_wide_sorted_and_keeps_offline(db):
    # Only users with an active private stream are enumerated; enrollment now
    # guarantees the sharer has one (see the next test), so the pair here is belt and braces.
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    sids = []
    for stamp in (10000, 10100, 10200):
        with patch("time.time", return_value=stamp):
            # A generous onlineUntil (vs. registered()'s default +60s) keeps the
            # surviving sessions' leases positioned for the 10300 listing time
            # below -- otherwise sids[0]'s default 60s lease (expires 10060) and
            # sids[2]'s (expires 10260) never overlap.
            sids.append(await registered(conversation=f"conv-{stamp}", onlineUntil=stamp + 500))
    rows = [await repo.get_session(OWNER, s) for s in sids]
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**rows[1], "endedAt": 10150, "lastEventAt": 10150}, rows[1], enrollment)
    # sids[0]'s lease lapses before listing time: it stays listed as offline so
    # a sleeping Mac reads as Offline instead of vanishing from the workspace list.
    await repo.put_session({**rows[0], "onlineUntil": 10200}, rows[0], enrollment)
    with patch("time.time", return_value=10300):
        listed = await service.list_workspace_sessions(BOB)
    assert [s.session_id for s in listed.sessions] == [sids[2], sids[0]]
    assert [s.status for s in listed.sessions] == ["idle", "offline"]
    assert all(s.status != "ended" for s in listed.sessions)
    # An outsider org sees nothing: the owner partition is the fence.
    with patch("time.time", return_value=10300):
        other = await service.list_workspace_sessions(AuthContext(user_id="user_x", org_id="org_other"))
    assert other.sessions == []


async def test_listing_the_workspace_reads_session_rows_in_batches_not_one_by_one(db, monkeypatch):
    """Prod 2026-10-07: one GET /messaging/sessions read each of 314 kept rows with its own
    GetItem (97 % of the table's reads), all queued at once on the 6-thread pool every other
    DynamoDB call of the process waits for. Relay frames behind them took up to 12 s."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    sids = [await registered(conversation=f"conv-{i}") for i in range(30)]
    real, hops = asyncio.to_thread, 0

    async def counted(func, *args, **kwargs):
        nonlocal hops
        hops += 1
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counted)
    listed = await service.list_workspace_sessions(BOB)
    assert {s.session_id for s in listed.sessions} == set(sids)
    assert hops <= 8, f"{hops} thread-pool calls to list 30 sessions"


async def test_list_workspace_sessions_include_ended_appends_the_last_seven_days(db):
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    now = 10_000_000
    with patch("time.time", return_value=now):
        live, recent, old = [await registered(conversation=c, onlineUntil=now + 500) for c in ("live", "recent", "old")]
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    for sid, ended in ((recent, now - 3600), (old, now - 8 * 24 * 3600)):
        row = await repo.get_session(OWNER, sid)
        await repo.put_session({**row, "endedAt": ended, "lastEventAt": ended, "eventSeq": 1}, row, enrollment)
    with patch("time.time", return_value=now):
        assert [s.session_id for s in (await service.list_workspace_sessions(BOB)).sessions] == [live]
        listed = await service.list_workspace_sessions(BOB, include_ended=True)
    # Live first, then only what ended inside the window; the week-old one stays out.
    assert [(s.session_id, s.status) for s in listed.sessions] == [(live, "idle"), (recent, "ended")]


async def test_list_workspace_sessions_enrolls_a_stream_and_keeps_offline_rows(db):
    # Enrollment (and first register) carries the sharer's private user stream,
    # so a desktop-only sharer is enumerable without ever opening chat.
    sid = await registered(conversation="conv-a")
    assert (await messaging_repo.get_user_stream(OWNER, ALICE.user_id))["active"] is True
    assert [s.session_id for s in (await service.list_workspace_sessions(BOB)).sessions] == [sid]
    # A legacy enrollment from before that change has no stream: nothing is enumerated.
    stream = await messaging_repo.get_user_stream(OWNER, ALICE.user_id)
    db[0].delete_item(Key={"pk": messaging_repo.owner_pk(OWNER), "sk": messaging_repo.userstream_sk(ALICE.user_id)})
    db[0].delete_item(Key={"pk": messaging_repo.stream_pk(stream["channelKey"]), "sk": "META"})
    assert (await service.list_workspace_sessions(BOB)).sessions == []
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    # Lease lapsed → status offline → still listed (see the previous test).
    with patch("time.time", return_value=int(time.time()) + 3600):
        listed = await service.list_workspace_sessions(BOB)
    assert [s.session_id for s in listed.sessions] == [sid]
    assert listed.sessions[0].status == "offline"


async def test_workspace_sessions_route_binds(db):
    from fastapi import FastAPI
    from routers import messaging

    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    sid = await registered(conversation="conv-http")
    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: BOB
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/messaging/sessions")
    assert response.status_code == 200
    body = response.json()
    assert [s["sessionId"] for s in body["sessions"]] == [sid]
    assert set(body["sessions"][0]) >= {"sessionId", "humanUserId", "status", "lastEventAt", "provider"}


async def test_single_session_route_reads_an_ended_session_for_any_member(db):
    from routers import messaging

    sid = await registered(conversation="conv-single")
    row = await repo.get_session(OWNER, sid)
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**row, "endedAt": int(time.time()), "lastEventAt": int(time.time())}, row, enrollment)
    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = app.dependency_overrides[get_human_session] = lambda: BOB
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/messaging/sessions/{sid}")
    assert response.status_code == 200
    assert response.json()["sessionId"] == sid and response.json()["status"] == "ended"
    with pytest.raises(ApiProblem) as excinfo:
        await service.get_session(AuthContext(user_id="user_x", org_id="org_other"), sid)
    assert excinfo.value.error.code == "LOCAL_SESSION_NOT_FOUND"


async def test_live_sessions_for_room_is_bounded_live_only_and_never_raises(db):
    sid = await registered(conversation="conv-agent")
    members = [
        {"kind": "human", "id": ALICE.user_id, "displayName": "A" * 300},
        {"kind": "human", "id": BOB.user_id, "displayName": "Bob"},
        {"kind": "agent", "id": "friday", "displayName": "Friday"},
    ]
    rows = await service.live_sessions_for_room(OWNER, ROOM, members)
    assert [r["sessionId"] for r in rows] == [sid]
    assert set(rows[0]) == {
        "sessionId",
        "humanUserId",
        "displayName",
        "provider",
        "cwdName",
        "title",
        "status",
        "latestEventSeq",
        "lastEventAt",
    }
    # The plugin (channel.ts) truncates on ingress; the backend passes it through whole.
    assert rows[0]["displayName"] == "A" * 300 and rows[0]["status"] in service.LIVE_STATUSES
    row = await repo.get_session(OWNER, sid)
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    await repo.put_session({**row, "endedAt": int(time.time()), "lastEventAt": int(time.time())}, row, enrollment)
    assert await service.live_sessions_for_room(OWNER, ROOM, members) == []
    with patch.object(repo, "list_user_sessions", side_effect=RuntimeError("boom")):
        assert await service.live_sessions_for_room(OWNER, ROOM, members) == []


async def test_background_jobs_are_left_out_of_human_listings_and_shown_to_agents(db, monkeypatch):
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent})
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    terminal = await registered(conversation="conv-terminal")
    job = await registered(conversation="conv-job")
    row = await repo.get_session(OWNER, job)
    await repo.put_session({**row, "kind": "background"}, row, await repo.get_enrollment(OWNER, ALICE.user_id, MAC))
    # Humans: neither the sidebar nor the room's resources carry the job.
    assert [s.session_id for s in (await service.list_workspace_sessions(BOB)).sessions] == [terminal]
    assert [s.session_id for s in (await service.list_resources(BOB, ROOM)).sessions] == [terminal]
    # The agent's resources tool sees both, labelled; a row written before kind existed reads interactive.
    caller = grants.verify(grants.mint(OWNER, ROOM, agent, "44444444-4444-4444-8444-444444444444"))
    listed = (await service.list_resources(caller, ROOM)).sessions
    assert {s.session_id: s.kind for s in listed} == {terminal: "interactive", job: "background"}
    members = [{"kind": "human", "id": ALICE.user_id, "displayName": "Alice"}]
    assert {r["sessionId"] for r in await service.live_sessions_for_room(OWNER, ROOM, members)} == {terminal, job}
    # Reads stay open: an agent may read the job it discovered.
    assert (await service.list_events(caller, ROOM, job)).events == []


async def test_live_sessions_for_room_does_not_truncate_a_long_human_user_id():
    # human_user_id carries no Field(max_length=...) anywhere in the LocalSession
    # chain, unlike title/cwd_name -- constructed directly rather than through the
    # moto fixture, which has no way to register a session under an over-long id.
    # The plugin (channel.ts) is the trust boundary and truncates on ingress;
    # the backend no longer duplicates that bound (truncating an id is wrong
    # anyway, and Clerk ids are ~32 chars).
    long_id = "x" * 200
    session = service.LocalSession(
        session_id="session-1",
        device_id=MAC,
        human_user_id=long_id,
        provider="codex",
        generation="generation-1",
        status="idle",
        provider_version="1.0",
        active_turn_id=None,
        capabilities=service.Capabilities(),
        capabilities_verified=True,
    )

    async def fake_live(_owner, _humans):
        return [session]

    members = [{"kind": "human", "id": long_id, "displayName": "Long"}]
    with patch.object(service, "_live_public_sessions", fake_live):
        rows = await service.live_sessions_for_room(OWNER, ROOM, members)
    assert rows[0]["humanUserId"] == long_id


async def test_live_sessions_for_room_blanks_for_a_foreign_member_without_querying(db):
    """The vendor shared channel (#2644): a foreign member's home differs from
    the room owner, so the whole summary is withheld -- and cheaply, before any
    session lookup runs (the guard now lives here, not in the caller)."""
    members = [
        {"kind": "human", "id": ALICE.user_id, "displayName": "Alice"},
        {"kind": "human", "id": "user_vendor", "displayName": "Vendor Rep", "homeOwnerId": "org_vendor"},
    ]
    with patch.object(
        repo, "list_user_sessions", side_effect=AssertionError("must not query when a member is foreign")
    ):
        assert await service.live_sessions_for_room(OWNER, ROOM, members) == []


async def test_stray_projection_never_attributes_another_humans_session(db):
    """A corrupt/stale projection under BOB's partition points at ALICE's real
    session. It must never surface ALICE's session as BOB's -- not duplicated
    into the workspace list, and not leaked into a room where only BOB is a
    member."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    sid = await registered(conversation="conv-alice")
    db[0].put_item(
        Item={
            "pk": repo.user_pk(OWNER, BOB.user_id),
            "sk": f"SESSION#{sid}",
            "ownerId": OWNER,
            "humanUserId": BOB.user_id,
            "deviceId": MAC,
            "sessionId": sid,
        }
    )
    listed = await service.list_workspace_sessions(BOB)
    assert [s.session_id for s in listed.sessions] == [sid]
    assert listed.sessions[0].human_user_id == ALICE.user_id

    assert (
        await service.live_sessions_for_room(OWNER, ROOM, [{"kind": "human", "id": BOB.user_id, "displayName": "Bob"}])
        == []
    )


async def test_live_listing_checks_enrollment_once_per_device(db):
    sids = {await registered(conversation=f"conv-{n}") for n in range(3)}
    calls = []
    real = service._enrolled

    async def counting(owner, human, device):
        calls.append((owner, human, device))
        return await real(owner, human, device)

    with patch.object(service, "_enrolled", counting):
        listed = await service._live_public_sessions(OWNER, [ALICE.user_id, BOB.user_id])
    assert {s.session_id for s in listed} == sids
    assert calls == [(OWNER, ALICE.user_id, MAC)]


async def test_agent_cannot_read_sessions_in_a_room_shared_with_another_org(db, monkeypatch):
    """The vendor shared channel (#2644): the customer's agent must not relay
    customer sessions to vendor staff. Humans keep their own org's access."""
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent})
    caller = grants.verify(grants.mint(OWNER, ROOM, agent, "44444444-4444-4444-8444-444444444444"))
    sid = await registered()
    assert [s.session_id for s in (await service.list_resources(caller, ROOM)).sessions] == [sid]
    db[0].put_item(
        Item={
            "pk": f"ROOM#{ROOM}",
            "sk": "MEMBER#human#user_vendor",
            "kind": "human",
            "id": "user_vendor",
            "ownerId": OWNER,
            "homeOwnerId": "org_vendor",
        }
    )
    with pytest.raises(ApiProblem) as refused:
        await service.list_resources(caller, ROOM)
    assert refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    assert [s.session_id for s in (await service.list_resources(ALICE, ROOM)).sessions] == [sid]


def test_session_terminal_trusts_the_mac_and_falls_back_to_its_holder():
    assert service.session_terminal({"terminal": "attach"}) == "attach"
    assert service.session_terminal({"terminal": "held"}) == "held"
    assert service.session_terminal({"terminal": "none", "holderId": "ab12cd34"}) is None
    assert service.session_terminal({"holderId": "ab12cd34"}) == "held", "a desktop that predates `terminal`"
    assert service.session_terminal({}) is None


def test_an_ended_origin_is_listed_once_as_its_successor():
    def s(sid, status, forked=None, human="alice", seq=1):
        return service.LocalSession.model_construct(
            session_id=sid,
            human_user_id=human,
            provider="claude",
            status=status,
            forked_from=forked,
            latest_event_seq=seq,
        )

    found = [
        s("new", "busy", "old"),
        s("old", "ended"),
        s("copy", "idle", "running"),
        s("running", "idle"),
        s("bobs", "ended", human="bob"),
        s("not-bobs", "idle", "bobs", human="mallory"),
        # A Mac draining an ended origin re-registers it, then lets it lapse: offline, not a second row.
        s("lapsed", "offline"),
        s("lapsed-fork", "busy", "lapsed"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == [
        "new",
        "copy",
        "running",
        "bobs",
        "not-bobs",
        "lapsed-fork",
    ]

    # A fork that ended before its first turn has no history and cannot be resumed: it is hidden,
    # and its origin (which holds the history) stays. A running empty fork stays (it has a terminal).
    found = [
        s("empty-fork", "ended", "origin", seq=0),
        s("origin", "ended"),
        s("live-empty", "busy", "origin2", seq=0),
        s("origin2", "ended"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == ["origin", "live-empty"]


@pytest.mark.asyncio
async def test_list_resources_lists_an_ended_origin_once_as_its_fork(db):
    origin, fork = await registered(conversation="origin"), await registered(conversation="fork")
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    row, now = await repo.get_session(OWNER, origin), int(time.time())
    await repo.put_session({**row, "endedAt": now, "lastEventAt": now}, row, enrollment)
    # The Mac records the origin on the fork (`forkedFrom`, see the relay test of the same name).
    row = await repo.get_session(OWNER, fork)
    await repo.put_session({**row, "forkedFrom": origin}, row, enrollment)
    resources = await service.list_resources(BOB, ROOM, include_ended=True)
    assert [(s.session_id, s.forked_from) for s in resources.sessions] == [(fork, origin)]


def test_a_session_that_ended_before_its_first_message_is_not_listed():
    def s(sid, status, seq, reason=None, provider="claude", held=None, on_purpose=None, forked_from=None):
        return service.LocalSession.model_construct(
            session_id=sid,
            human_user_id="alice",
            provider=provider,
            status=status,
            latest_event_seq=seq,
            control_unavailable_reason=reason,
            was_held=held,
            ended_on_purpose=on_purpose,
            forked_from=forked_from,
        )

    found = [
        s("empty", "ended", 0),
        s("starting", "idle", 0),
        s("used", "ended", 3),
        # The Mac could not read its history: it may hold a real conversation.
        s("unread", "ended", 0, "history-gap"),
        # Codex keeps the thread in its rollout before any event is public: `codex resume` finds it.
        s("codex", "ended", 0, provider="codex"),
        # It had a live terminal: its first events take up to a minute to arrive, so zero events
        # does not mean nothing happened in it.
        s("held", "ended", 0, held=True),
        # Ended on purpose (owner ruling 2026-10-08): never listed, whatever it holds.
        s("left", "ended", 3, on_purpose=True),
        s("left-held", "ended", 0, held=True, on_purpose=True),
        s("left-codex", "ended", 0, provider="codex", on_purpose=True),
        # A fork ended on purpose still stands in for its origin: the origin does not come back.
        s("origin", "ended", 5),
        s("fork", "ended", 2, on_purpose=True, forked_from="origin"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == ["starting", "used", "unread", "codex", "held"]


@pytest.mark.asyncio
async def test_a_held_session_ended_before_its_first_event_stays_listed(db):
    """Prod 2026-10-08: a held session ended in its first minute left the list, and the panel
    open on it lost its row and remounted the terminal. A plain one that never ran is still hidden."""
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    held = await registered(conversation="held", provider="claude")
    plain = await registered(conversation="plain", provider="claude")
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    now = int(time.time())
    for sid, holder in ((held, {"holderId": "ab12cd34"}), (plain, {})):
        row = await repo.get_session(OWNER, sid)
        await repo.put_session({**row, **holder, "endedAt": now, "lastEventAt": now}, row, enrollment)
    listed = (await service.list_workspace_sessions(BOB, include_ended=True)).sessions
    assert [(x.session_id, x.status, x.latest_event_seq, x.was_held) for x in listed] == [(held, "ended", 0, True)]
    # Listed, but still nothing to resume: Claude wrote no transcript we know of.
    assert listed[0].resumable is False


@pytest.mark.asyncio
async def test_an_empty_ended_session_is_not_resumable_but_stays_readable(db, monkeypatch):
    from core.services import session_resume

    monkeypatch.setattr(session_resume, "submissions_enabled", lambda owner: True)
    empty = await registered(conversation="empty", provider="claude")
    used = await registered(conversation="used", provider="claude")
    # Codex keeps the thread in its rollout before any event is public: still resumable and listed.
    codex = await registered(conversation="codex-empty")
    enrollment = await repo.get_enrollment(OWNER, ALICE.user_id, MAC)
    now = int(time.time())
    for sid, seq in ((empty, 0), (used, 2), (codex, 0)):
        row = await repo.get_session(OWNER, sid)
        await repo.put_session(
            {**row, "endedAt": now, "lastEventAt": now, "eventSeq": seq, "resumeAllowed": [BOB.user_id]},
            row,
            enrollment,
        )
    public = service._session_public(await repo.get_session(OWNER, empty), ALICE.user_id)
    assert public.resumable is False and public.resume_allowed == []
    public = service._session_public(await repo.get_session(OWNER, used), ALICE.user_id)
    assert public.resumable is True and public.resume_allowed == [BOB.user_id]
    resources = await service.list_resources(BOB, ROOM, include_ended=True)
    assert sorted(x.session_id for x in resources.sessions) == sorted([used, codex])
    assert service._session_public(await repo.get_session(OWNER, codex), ALICE.user_id).resumable is True
    assert (await service.get_session(BOB, empty)).session_id == empty
