"""Transport integration: real Moto transactions/connections, stub only outbound HTTP."""

import asyncio
import importlib
import json
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service
from core.services import native_session_tools as native_tools
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_sessions as registry_tests
from tests.unit.services.test_local_sessions import ALICE, BOB, CLI, MAC, OTHER_ROOM, OWNER, ROOM, command

db = registry_tests.db
ORIGIN_MESSAGE_ID = "33333333-3333-4333-8333-333333333333"
ORIGIN_ROOT_ID = "44444444-4444-4444-8444-444444444444"
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

    async def publish_http(_self, endpoint, body, headers):
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


def _mention_outcome(sid, request_id, outcome="applied", provider_id="provider-1", held=False):
    return {
        "type": "local.session.receipt",
        "sessionId": sid,
        "generation": "generation-1",
        "requestId": request_id,
        "outcome": outcome,
        "providerMessageId": provider_id if outcome == "applied" else None,
        "held": held,
    }


def _reply_events(sid, provider_id="provider-1", text="All done."):
    return {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": "reply-batch",
        "afterSeq": 0,
        "events": [
            {"kind": "text", "role": "user", "text": "the prompt", "itemId": provider_id},
            {"kind": "text", "role": "assistant", "text": text, "itemId": "assistant-1"},
            {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "turn_end", "itemId": "turn-1"},
        ],
    }


def test_codex_reply_prompt_prefers_exact_request_id_and_preserves_legacy_fallback():
    module = importlib.import_module("core.services.local_session_relay")
    event = service.LocalSessionEventBody(kind="text", role="user", text="prompt", origin_request_id="mention-1")
    assert event.model_dump(by_alias=True)["originRequestId"] == "mention-1"
    pointer = {"provider": "codex", "requestId": "mention-1", "providerMessageId": "queue-1"}
    assert module._matches_reply_prompt({"role": "user", "itemId": "native-1", "originRequestId": "mention-1"}, pointer)
    assert not module._matches_reply_prompt(
        {"role": "user", "itemId": "queue-1", "originRequestId": "other-mention"}, pointer
    )
    assert module._matches_reply_prompt({"role": "user", "itemId": "queue-1"}, pointer)
    assert not module._matches_reply_prompt({"role": "user", "itemId": "native-1"}, pointer)
    assert module._matches_reply_prompt(
        {"role": "user", "itemId": "native-claude"},
        {"provider": "claude", "requestId": "mention-1", "providerMessageId": "native-claude"},
    )


async def _upload_reply_events(transport, frame):
    # These cases exercise the heartbeat/Ended retry paths; leave the new
    # immediate turn-end worker to its own case rather than racing it here.
    module = importlib.import_module("core.services.local_session_relay")
    with patch.object(module, "_spawn"):
        await relay(transport).handle_message("conn-1", frame)


async def _applied_mention(transport, outcome="applied", *, thread=False, sender=ALICE, held=False):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, roomReplyRequestId=True))
    table = messaging_repo._table()
    if sender != ALICE:
        table.update_item(
            Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
            UpdateExpression="SET teamControl = :yes",
            ExpressionAttributeValues={":yes": True},
        )
    if thread:
        table.put_item(
            Item={
                "pk": messaging_repo.room_pk(ROOM),
                "sk": messaging_repo.msg_sk(1),
                "messageId": ORIGIN_ROOT_ID,
                "roomId": ROOM,
                "messageSeq": 1,
                "threadRootMessageId": None,
                "author": {"kind": "human", "id": sender.user_id},
            }
        )
    origin_seq = 2 if thread else 1
    table.put_item(
        Item={
            "pk": messaging_repo.room_pk(ROOM),
            "sk": messaging_repo.msg_sk(origin_seq),
            "messageId": ORIGIN_MESSAGE_ID,
            "roomId": ROOM,
            "messageSeq": origin_seq,
            "threadRootMessageId": ORIGIN_ROOT_ID if thread else None,
            **({"threadRootSeq": 1} if thread else {}),
            "author": {"kind": "human", "id": sender.user_id},
        }
    )
    table.update_item(
        Key={"pk": messaging_repo.room_pk(ROOM), "sk": "META"},
        UpdateExpression="SET lastSeq = :seq, topCount = :one",
        ExpressionAttributeValues={":seq": origin_seq, ":one": 1},
    )
    receipt = await service.submit_command(
        sender,
        None,
        sid,
        command(service, requestId="mention-1"),
        origin_room_id=ROOM,
        origin_message_id=ORIGIN_MESSAGE_ID,
    )
    # This helper tests reply wiring, not the five-second dispatch claim. A loaded
    # CI runner can exhaust that claim before this fixture sends its fake receipt.
    with patch("core.services.local_session_relay.CLAIM_SECONDS", 30):
        await relay(transport).dispatch_session(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, receipt.request_id))["state"] == "dispatched"
    await relay(transport).handle_message("conn-1", _mention_outcome(sid, receipt.request_id, outcome, held=held))
    return sid, receipt


@pytest.mark.asyncio
async def test_codex_room_mention_requires_new_desktop_reply_correlation(transport):
    sid = await start(transport)
    with pytest.raises(ApiProblem) as error:
        await service.submit_command(
            ALICE,
            None,
            sid,
            command(service, requestId="old-desktop-mention"),
            origin_room_id=ROOM,
            origin_message_id=ORIGIN_MESSAGE_ID,
        )
    assert error.value.error.code == "LOCAL_SESSION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_queued_codex_mention_is_rejected_if_reconnected_desktop_loses_reply_correlation(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, roomReplyRequestId=True))
    receipt = await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="mention-before-reconnect"),
        origin_room_id=ROOM,
        origin_message_id=ORIGIN_MESSAGE_ID,
    )
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", registration())
    assert (await repo.get_command(OWNER, sid, receipt.request_id))["state"] == "rejected"
    assert not any(body["type"] == "local.session.command" for _, body in transport[3])


@pytest.mark.asyncio
async def test_applied_mention_receipt_writes_pointer_once(transport):
    sid, receipt = await _applied_mention(transport)
    pointers = await repo.list_replyto(OWNER, sid)
    assert len(pointers) == 1
    pointer = pointers[0]
    assert pointer["roomId"] == ROOM and pointer["messageId"] == ORIGIN_MESSAGE_ID
    assert pointer["requestId"] == receipt.request_id
    assert pointer["providerMessageId"] == "provider-1"
    await relay(transport).handle_message("conn-1", _mention_outcome(sid, receipt.request_id))
    assert await repo.list_replyto(OWNER, sid) == pointers  # replay does not re-arm anything


@pytest.mark.asyncio
async def test_rejected_mention_receipt_writes_no_reply_pointer(transport):
    sid, _ = await _applied_mention(transport, outcome="rejected")
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_busy_to_idle_posts_one_silent_session_reply(transport):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid))
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="idle", activeTurnId=None, lastAssistantMessage="All done.")
    )
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    replies = [m for m in messages if m["author"]["kind"] == "system"]
    assert len(replies) == 1
    assert replies[0]["silent"] is True and replies[0]["dispatchTargets"] == []
    assert replies[0]["mentions"] == []
    assert "All done." in replies[0]["text"] and f"session={sid}" in replies[0]["text"]
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_room_reply_includes_every_block_of_the_final_native_answer(transport):
    sid, _ = await _applied_mention(transport)
    batch = _reply_events(sid, text="First block. " * 35 + "https://exam")
    batch["events"].insert(1, {"kind": "text", "role": "assistant", "text": "Working note.", "itemId": "commentary"})
    batch["events"].insert(-1, {"kind": "text", "role": "assistant", "text": "ple.com", "itemId": "assistant-1"})
    await _upload_reply_events(transport, batch)
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    reply = next(message for message in messages if message["author"]["kind"] == "system")
    assert reply["text"].count("First block.") == 35
    assert "https://example.com" in reply["text"]
    assert "Working note." not in reply["text"]
    assert reply["threadRootMessageId"] == ORIGIN_MESSAGE_ID
    assert reply["replyToMessageId"] == ORIGIN_MESSAGE_ID
    assert reply["threadRootSeq"] == 1


@pytest.mark.asyncio
async def test_final_transcript_batch_posts_reply_before_an_idle_heartbeat(transport):
    sid, _ = await _applied_mention(transport)
    await relay(transport).handle_message("conn-1", _reply_events(sid))
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([m for m in messages if m["author"]["kind"] == "system"]) == 1
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_codex_room_reply_uses_request_id_across_distinct_queue_and_native_ids(transport, db):
    sid, receipt = await _applied_mention(transport, held=True)
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": f"REPLY#{receipt.request_id}"},
        UpdateExpression="SET provider = :provider",
        ExpressionAttributeValues={":provider": "codex"},
    )
    batch = _reply_events(sid, provider_id="native-user", text="The right answer.")
    batch["events"] = [
        {
            "kind": "text",
            "role": "user",
            "text": "same queue id",
            "itemId": "provider-1",
            "originRequestId": "another-request",
        },
        {"kind": "text", "role": "assistant", "text": "The wrong answer."},
        {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "turn_end"},
        {**batch["events"][0], "originRequestId": receipt.request_id},
        *batch["events"][1:],
    ]
    await relay(transport).handle_message("conn-1", batch)
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    replies = [m for m in messages if m["author"]["kind"] == "system"]
    assert len(replies) == 1 and "The right answer." in replies[0]["text"]
    assert "The wrong answer." not in replies[0]["text"]
    assert await repo.list_replyto(OWNER, sid) == []
    completed = await service.get_command(ALICE, None, sid, receipt.request_id)
    assert completed.reply_posted is True and completed.held is True


@pytest.mark.asyncio
async def test_thread_mention_reply_stays_in_its_originating_thread(transport):
    sid, _ = await _applied_mention(transport, thread=True)
    await _upload_reply_events(transport, _reply_events(sid, text="Thread answer."))
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="idle", activeTurnId=None, lastAssistantMessage="Thread answer.")
    )
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    reply = next(m for m in messages if m["author"]["kind"] == "system")
    assert reply["threadRootMessageId"] == ORIGIN_ROOT_ID
    assert reply["replyToMessageId"] == ORIGIN_MESSAGE_ID
    assert reply["threadRootSeq"] == 1
    assert reply["author"]["displayName"].startswith("Codex · ")
    assert reply["text"].startswith("Thread answer.")


@pytest.mark.asyncio
async def test_exact_mention_reply_marks_the_held_receipt_complete_idempotently(transport):
    sid, receipt = await _applied_mention(transport, held=True)
    stored = await repo.get_command(OWNER, sid, receipt.request_id)
    assert stored["held"] is True
    await _upload_reply_events(transport, _reply_events(sid, text="Native answer."))
    module = importlib.import_module("core.services.local_session_relay")

    await module.post_session_replies(await repo.get_session(OWNER, sid))
    completed = await service.get_command(ALICE, None, sid, receipt.request_id)
    assert completed.held is True
    assert completed.reply_posted is True
    assert await repo.list_replyto(OWNER, sid) == []

    # A replay after pointer cleanup cannot duplicate the canonical room reply.
    await module.post_session_replies(await repo.get_session(OWNER, sid))
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([message for message in messages if message["author"]["kind"] == "system"]) == 1


@pytest.mark.asyncio
async def test_reply_marker_failure_keeps_pointer_for_idempotent_retry(transport, monkeypatch):
    sid, receipt = await _applied_mention(transport, held=True)
    await _upload_reply_events(transport, _reply_events(sid, text="Retry answer."))
    original = repo.replace_command
    failed = False

    async def fail_once(previous, updated, *, checks=(), extra=()):
        nonlocal failed
        if updated.get("replyPosted") and not failed:
            failed = True
            raise RuntimeError("simulated marker write failure")
        return await original(previous, updated, checks=checks, extra=extra)

    monkeypatch.setattr(repo, "replace_command", fail_once)
    module = importlib.import_module("core.services.local_session_relay")
    row = await repo.get_session(OWNER, sid)
    await module.post_session_replies(row)
    assert await repo.list_replyto(OWNER, sid)
    assert (await repo.get_command(OWNER, sid, receipt.request_id)).get("replyPosted") is not True

    await module.post_session_replies(row)
    completed = await service.get_command(ALICE, None, sid, receipt.request_id)
    assert completed.reply_posted is True and completed.held is True
    assert await repo.list_replyto(OWNER, sid) == []
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([message for message in messages if message["author"]["kind"] == "system"]) == 1


@pytest.mark.asyncio
async def test_unmentioned_session_idle_heartbeat_posts_no_reply(transport):
    sid = await start(transport)
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="idle", activeTurnId=None, lastAssistantMessage="Unrelated.")
    )
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert not any(m["author"]["kind"] == "system" for m in messages)


@pytest.mark.asyncio
async def test_reply_is_suppressed_if_the_room_became_foreign(transport, db):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid, text="Private reply."))
    db[0].put_item(
        Item={
            "pk": messaging_repo.room_pk(ROOM),
            "sk": messaging_repo.member_sk("human", "outsider"),
            "kind": "human",
            "id": "outsider",
            "homeOwnerId": "other-workspace",
        }
    )
    await relay(transport).handle_message(
        "conn-1", heartbeat(sid, status="idle", activeTurnId=None, lastAssistantMessage="Private reply.")
    )
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert not any(m["author"]["kind"] == "system" for m in messages)


@pytest.mark.asyncio
async def test_room_join_after_access_check_cannot_receive_native_answer(transport, db, monkeypatch):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid, text="private answer " * 40))
    original = messaging_repo.has_foreign_members

    async def join_after_check(room_id, owner_id):
        result = await original(room_id, owner_id)
        db[0].put_item(
            Item={
                "pk": messaging_repo.room_pk(ROOM),
                "sk": messaging_repo.member_sk("human", "outsider"),
                "kind": "human",
                "id": "outsider",
                "homeOwnerId": "other-workspace",
            }
        )
        db[0].update_item(
            Key={"pk": messaging_repo.room_pk(ROOM), "sk": "META"},
            UpdateExpression="ADD revision :one",
            ExpressionAttributeValues={":one": 1},
        )
        return result

    monkeypatch.setattr(messaging_repo, "has_foreign_members", join_after_check)
    module = importlib.import_module("core.services.local_session_relay")
    await module.post_session_replies(await repo.get_session(OWNER, sid))
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert not any(m["author"]["kind"] == "system" for m in messages)
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_room_revision_race_rechecks_access_and_posts_once(transport, db, monkeypatch):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid, text="Full answer."))
    original = messaging_repo.has_foreign_members
    changed = False

    async def change_room_revision(room_id, owner_id):
        nonlocal changed
        result = await original(room_id, owner_id)
        if not changed:
            changed = True
            db[0].update_item(
                Key={"pk": messaging_repo.room_pk(ROOM), "sk": "META"},
                UpdateExpression="ADD revision :one",
                ExpressionAttributeValues={":one": 1},
            )
        return result

    monkeypatch.setattr(messaging_repo, "has_foreign_members", change_room_revision)
    module = importlib.import_module("core.services.local_session_relay")
    await module.post_session_replies(await repo.get_session(OWNER, sid))
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    replies = [message for message in messages if message["author"]["kind"] == "system"]
    assert len(replies) == 1
    assert "Full answer." in replies[0]["text"]
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_teammate_owned_session_replies_when_sender_remains_in_room(transport, db):
    sid, _ = await _applied_mention(transport, sender=BOB)
    db[0].delete_item(Key={"pk": messaging_repo.room_pk(ROOM), "sk": messaging_repo.member_sk("human", ALICE.user_id)})
    await _upload_reply_events(transport, _reply_events(sid))
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([m for m in messages if m["author"]["kind"] == "system"]) == 1
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_two_queued_commands_reply_to_their_own_rooms(transport):
    sid, _ = await _applied_mention(transport)
    second_id = "55555555-5555-4555-8555-555555555555"
    table = messaging_repo._table()
    table.put_item(
        Item={
            "pk": messaging_repo.room_pk(OTHER_ROOM),
            "sk": messaging_repo.msg_sk(1),
            "messageId": second_id,
            "roomId": OTHER_ROOM,
            "messageSeq": 1,
            "threadRootMessageId": None,
            "author": {"kind": "human", "id": ALICE.user_id},
        }
    )
    table.update_item(
        Key={"pk": messaging_repo.room_pk(OTHER_ROOM), "sk": "META"},
        UpdateExpression="SET lastSeq = :seq, topCount = :seq",
        ExpressionAttributeValues={":seq": 1},
    )
    second = await service.submit_command(
        ALICE,
        None,
        sid,
        command(service, requestId="mention-2"),
        origin_room_id=OTHER_ROOM,
        origin_message_id=second_id,
    )
    await relay(transport).dispatch_session(OWNER, sid)
    await relay(transport).handle_message("conn-1", _mention_outcome(sid, second.request_id, provider_id="provider-2"))
    assert len(await repo.list_replyto(OWNER, sid)) == 2
    batch = _reply_events(sid)
    batch["events"].extend(
        [
            {"kind": "text", "role": "user", "text": "second prompt", "itemId": "provider-2"},
            {"kind": "text", "role": "assistant", "text": "Second answer.", "itemId": "assistant-2"},
            {"kind": "lifecycle", "role": "system", "text": "", "lifecycle": "turn_end", "itemId": "turn-2"},
        ]
    )
    await _upload_reply_events(transport, batch)
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    module = importlib.import_module("core.services.local_session_relay")
    await asyncio.gather(*module._tasks)
    first_room = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    second_room = await messaging_repo._query(messaging_repo.room_pk(OTHER_ROOM), begins_with="MSG#")
    assert [m["text"] for m in first_room if m["author"]["kind"] == "system"][0].find("All done.") >= 0
    assert [m["text"] for m in second_room if m["author"]["kind"] == "system"][0].find("Second answer.") >= 0
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_two_idle_workers_post_one_line_for_one_command(transport):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid))
    module = importlib.import_module("core.services.local_session_relay")
    row = await repo.get_session(OWNER, sid)
    await asyncio.gather(module.post_session_replies(row), module.post_session_replies(row))
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([m for m in messages if m["author"]["kind"] == "system"]) == 1
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_idle_heartbeat_retries_after_room_write_failure(transport, monkeypatch):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid))
    real = messaging_repo.create_message
    attempts = 0

    async def flaky(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await real(**kwargs)
            raise RuntimeError("lost room write response")
        return await real(**kwargs)

    monkeypatch.setattr(messaging_repo, "create_message", flaky)
    module = importlib.import_module("core.services.local_session_relay")
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    await asyncio.gather(*module._tasks)
    assert len(await repo.list_replyto(OWNER, sid)) == 1
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    await asyncio.gather(*module._tasks)
    assert attempts == 2 and await repo.list_replyto(OWNER, sid) == []
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([m for m in messages if m["author"]["kind"] == "system"]) == 1


@pytest.mark.asyncio
async def test_idle_heartbeat_waits_for_transcript_then_retries(transport):
    sid, _ = await _applied_mention(transport)
    module = importlib.import_module("core.services.local_session_relay")
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    await asyncio.gather(*module._tasks)
    assert len(await repo.list_replyto(OWNER, sid)) == 1
    await _upload_reply_events(transport, _reply_events(sid))
    await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle", activeTurnId=None))
    await asyncio.gather(*module._tasks)
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_ended_session_flushes_a_completed_reply_without_another_heartbeat(transport):
    sid, _ = await _applied_mention(transport)
    module = importlib.import_module("core.services.local_session_relay")
    with patch.object(module, "_spawn"):
        await relay(transport).handle_message("conn-1", _reply_events(sid))
    await relay(transport).handle_message(
        "conn-1",
        {
            "type": "local.session.ended",
            "sessionId": sid,
            "generation": "generation-1",
            "reason": "exited",
            "endedAt": int(time.time()),
        },
    )
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert len([m for m in messages if m["author"]["kind"] == "system"]) == 1
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
async def test_replayed_ended_ack_retries_a_lost_reply_write(transport, monkeypatch):
    sid, _ = await _applied_mention(transport)
    await _upload_reply_events(transport, _reply_events(sid))
    real = messaging_repo.create_message
    attempts = 0

    async def flaky(**kwargs):
        nonlocal attempts
        attempts += 1
        result = await real(**kwargs)
        if attempts == 1:
            raise RuntimeError("lost response")
        return result

    monkeypatch.setattr(messaging_repo, "create_message", flaky)
    frame = {
        "type": "local.session.ended",
        "sessionId": sid,
        "generation": "generation-1",
        "reason": "exited",
        "endedAt": int(time.time()),
    }
    module = importlib.import_module("core.services.local_session_relay")
    await relay(transport).handle_message("conn-1", frame)
    await asyncio.gather(*module._tasks)
    assert len(await repo.list_replyto(OWNER, sid)) == 1
    await relay(transport).handle_message("conn-1", frame)
    await asyncio.gather(*module._tasks)
    messages = await messaging_repo._query(messaging_repo.room_pk(ROOM), begins_with="MSG#")
    assert attempts == 2 and len([m for m in messages if m["author"]["kind"] == "system"]) == 1
    assert await repo.list_replyto(OWNER, sid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "member", "expiry"])
async def test_agent_dispatch_uses_typed_author_and_fences_revocation_or_expiry(transport, db, monkeypatch, change):
    from core.services import local_session_grants as grants

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": "MEMBER#agent#friday", "kind": "agent", "id": "friday"})
    caller = grants.verify(grants.mint(OWNER, ROOM, "friday", OTHER_ROOM, ALICE.user_id))
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
    assert "replyPosted" not in commands[0]["command"]


@pytest.mark.asyncio
async def test_room_mention_labels_sender_without_making_command_room_scoped(transport, db):
    db[0].update_item(
        Key={"pk": messaging_repo.room_pk(ROOM), "sk": messaging_repo.member_sk("human", BOB.user_id)},
        UpdateExpression="SET displayName = :name",
        ExpressionAttributeValues={":name": "Bob Example"},
    )
    await _applied_mention(transport, sender=BOB)
    commands = [body for _, body in transport[3] if body["type"] == "local.session.command"]
    assert len(commands) == 1
    assert commands[0]["command"]["roomId"] == ""
    assert commands[0]["roomMention"] is True
    assert commands[0]["actor"] == {"kind": "human", "handle": "bob-example", "displayName": "Bob Example"}
    assert "replyPosted" not in commands[0]["command"]


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
        # One workspace hint on each member's own channel; no per-room fan-out (2026-09-24).
        assert "/users/" in hint["channel"]
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
async def test_same_generation_reconnect_restores_pending_native_send(transport):
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    frame = registration(provider="claude", nativeConversationId="claude-1", activeTurnId=None, status="idle")
    sid = (await relay(transport).handle_message("conn-1", frame))["sessionId"]
    request_id, approval_id = str(uuid.uuid4()), "appr-" + uuid.uuid4().hex
    destination, text = f"room:{ROOM}", "private text"
    digest = native_tools._intent_digest({"destination": destination, "text": text, "requestId": request_id})
    expires = int(time.time()) + 3300
    pointer = {
        "requestId": request_id,
        "approvalId": approval_id,
        "inputDigest": digest,
        "expiresAt": expires,
        "state": "pending_approval",
        "connectionId": "conn-1",
        "sourceGeneration": "generation-1",
        "sourceTurnId": None,
    }
    table = messaging_repo._table()
    pk = repo.session_pk(OWNER, sid)
    table.update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression="SET nativeSendIntent = :intent",
        ExpressionAttributeValues={":intent": pointer},
    )
    table.put_item(
        Item={
            "pk": pk,
            "sk": f"NATIVE_INTENT#{request_id}",
            "requestId": request_id,
            "approvalId": approval_id,
            "inputDigest": digest,
            "destination": destination,
            "text": text,
            "sourceGeneration": "generation-1",
            "sourceConversationId": "claude-1",
            "deviceId": MAC,
            "humanUserId": ALICE.user_id,
            "sourceTurnId": None,
            "connectionId": "conn-1",
            "state": "pending_approval",
            "expiresAt": expires,
        }
    )
    table.put_item(
        Item={
            "pk": pk,
            "sk": f"NATIVE_TOOL#{request_id}",
            "approvalId": approval_id,
            "inputDigest": digest,
            "destination": destination,
            "approvalExpiresAt": expires,
            "requestHash": messaging_repo.request_hash(sid, destination, text),
        }
    )

    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", frame)

    row = await repo.get_session(OWNER, sid)
    assert row["nativeSendIntent"] == {**pointer, "connectionId": "conn-2"}
    assert table.get_item(Key={"pk": pk, "sk": f"NATIVE_INTENT#{request_id}"})["Item"]["connectionId"] == "conn-2"
    public = service._session_public(row, ALICE.user_id)
    assert public.pending_approval.approval_id == approval_id
    assert (await service.get_private_send_approval(ALICE, sid)).text == text
    assert (await native_tools.get_codex_send_receipt(ALICE, sid, request_id))["state"] == "pending_approval"
    with pytest.raises(ApiProblem):
        await native_tools.answer_codex_send(
            ALICE,
            sid,
            native_tools.NativeSendDecision(
                request_id=request_id, approval_id=approval_id, input_digest="f" * 64, decision="deny"
            ),
        )
    denied = await native_tools.answer_codex_send(
        ALICE,
        sid,
        native_tools.NativeSendDecision(
            request_id=request_id, approval_id=approval_id, input_digest=digest, decision="deny"
        ),
    )
    assert denied["state"] == "rejected" and denied["error"]["code"] == "OWNER_DENIED"


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
async def test_offline_queue_excludes_room_mentions_and_live_controls(transport):
    sid = await start(transport)
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET onlineUntil = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    with pytest.raises(ApiProblem, match="offline"):
        await service.submit_command(ALICE, None, sid, command(service), origin_room_id=ROOM)
    with pytest.raises(ApiProblem, match="offline"):
        await service.submit_command(
            ALICE,
            None,
            sid,
            command(service, requestId="native-offline"),
            source_session_id=sid,
            source_generation="generation-1",
            native_hop=1,
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
    notice.assert_awaited_once_with(OWNER, ALICE.user_id)
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
        first = [h for h in transport[4] if "/users/" in h["channel"]]
        assert len(first) == 1
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b2", "afterSeq": 1, "events": [lifecycle]})
        assert len([h for h in transport[4] if "/users/" in h["channel"]]) == 1
    with patch("time.time", return_value=2000000002.0):
        await relay(transport).handle_message("conn-1", {**body, "batchId": "b3", "afterSeq": 2, "events": [lifecycle]})
    assert len([h for h in transport[4] if "/users/" in h["channel"]]) == 2


@pytest.mark.asyncio
async def test_router_dispatches_post_but_receipt_get_is_read_only(transport):
    from core.auth import get_current_user
    from routers.messaging import router

    sid = await start(transport)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: ALICE
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
    assert any("/users/" in hint["channel"] for hint in transport[4])
    connect(transport, "conn-2", "2000")
    await relay(transport).handle_message("conn-2", registration())
    assert (await repo.get_session(OWNER, sid))["title"] == "Renamed by the harness"
    await relay(transport).handle_message(
        "conn-2", {**registration(generation="generation-2"), "previousGeneration": "generation-1"}
    )
    assert (await repo.get_session(OWNER, sid)).get("title") is None


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
    app.dependency_overrides[get_current_user] = lambda: ALICE
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
    assert not any("/rooms/" in h["channel"] for h in transport[4])


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
    hints = [json.loads(h["events"][0]) for h in transport[4] if "/users/" in h["channel"]]
    assert len(hints) == 2
    assert all(e["payload"] == {"humanUserId": ALICE.user_id, "transcript": True} for e in hints)
    assert not any("/rooms/" in h["channel"] for h in transport[4])


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
    # A lost ack replays idempotently: the notice row carries the batch hash.
    assert await relay(transport).handle_message("conn-1", batch(1, 0)) == first
    assert (await relay(transport).handle_message("conn-1", batch(2, 2)))["afterSeq"] == 4
    resumed = await service.set_sharing(ALICE, sid, True)
    assert resumed.shared is True and resumed.latest_event_seq == 4
    await relay(transport).handle_message("conn-1", batch(3, 4))
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

    async def push(user_id, title, body, data=None):
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
    teams = lambda: (_ for _ in ()).throw(RuntimeError("no paperclip"))  # noqa: E731
    with patch("time.time", return_value=10200):
        page = await activity.list_activity(ALICE, cursor=None, limit=30, teams_ctx=teams)
    session_items = [i for i in page.items if i.kind == "session"]
    assert [(i.subtype, i.state) for i in session_items] == [("finished", "open"), ("waiting", "resolved")]
    assert session_items[0].actions[0].navigate.kind == "session" and session_items[0].actions[0].navigate.id == sid
    assert page.attention_count == 0  # a finished turn never badges; the wait is over

    with patch("core.services.push_service.send_push_to_user", push), patch("time.time", return_value=10300):
        await relay(transport).handle_message("conn-1", heartbeat(sid, status="waiting-approval"))
        await asyncio.gather(*module._tasks)
        page = await activity.list_activity(ALICE, cursor=None, limit=30, teams_ctx=teams)
    assert page.items[0].subtype == "waiting" and page.items[0].state == "open"
    assert page.attention_count == 1
    # Nobody else is told: Bob's feed has no session items.
    with patch("time.time", return_value=10300):
        bob = await activity.list_activity(BOB, cursor=None, limit=30, teams_ctx=teams)
    assert not [i for i in bob.items if i.kind == "session"]


async def test_subagent_events_round_trip_and_a_long_background_end_notifies_once(transport):
    from core.services import activity, local_session_relay as module

    assert "sessionSubagents" in module.FEATURES
    pushes = []

    async def push(user_id, title, body, data=None):
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

    teams = lambda: (_ for _ in ()).throw(RuntimeError("no paperclip"))  # noqa: E731
    with patch("time.time", return_value=10500):
        feed = await activity.list_activity(ALICE, cursor=None, limit=30, teams_ctx=teams)
    assert len([i for i in feed.items if i.kind == "session"]) == 3
