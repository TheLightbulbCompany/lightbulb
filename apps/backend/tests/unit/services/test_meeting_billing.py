"""Meeting duration is durably admitted before delivery; speech retains its credit accounting."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from core.services import voice_calls as vc  # noqa: E402
from routers import meetings  # noqa: E402

ROOM = "11111111-1111-4111-8111-111111111111"


async def test_presence_is_durably_admitted_and_speech_keeps_credit_accounting(monkeypatch):
    # Presence: 1800 seconds is durably admitted for later delivery, never ingested inline.
    meta = {"roomId": ROOM, "ownerId": "org_a", "meeting": {"status": "bot_created", "recallBotId": "bot-1"}}

    async def patch_meeting(room_id, fields, drop=()):
        meta["meeting"] = {**meta["meeting"], **fields}
        return meta["meeting"]

    clock = MagicMock(return_value=1000.0)
    metronome = MagicMock(ingest=AsyncMock())
    monkeypatch.setattr(vc, "_metronome", MagicMock(return_value=metronome))
    with (
        patch.object(meetings.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta)),
        patch.object(meetings.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting)),
        patch.object(meetings.meeting_invites.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="t")),
        patch.object(meetings.meeting_invites.container_repo, "release_owner_lifecycle_claim", AsyncMock()),
        patch.object(meetings.clerk_admin, "get_organization", AsyncMock(return_value={})),
        patch.object(meetings.billing_repo, "get_by_owner_id", AsyncMock(return_value={})),
        patch.object(meetings.voice_billing.messaging_repo, "put_voice_usage_event", AsyncMock()) as admission,
        patch.object(meetings.voice_billing, "add_month_seconds", AsyncMock()) as allowance,
        patch.object(meetings.livekit_huddles, "dispatch_meet", AsyncMock()),
        patch.object(meetings.livekit_huddles, "close_room", AsyncMock()),
        patch.object(meetings.voice_calls, "room_agents", AsyncMock(return_value=[])),
        patch.object(meetings.voice_calls, "prewake_owner", MagicMock()),
        patch.object(meetings, "get_live_event_publisher", return_value=MagicMock(publish_room=AsyncMock())),
        patch.object(meetings.meeting_notes, "on_ended", AsyncMock()),
        patch.object(meetings, "put_metric") as metric,
        patch.object(meetings.time, "time", clock),
    ):
        await meetings._on_status("bot-1", ROOM, "in_call_recording", "")
        clock.return_value = 2800.0
        await meetings._on_status("bot-1", ROOM, "call_ended", "")
    metric.assert_any_call("meeting.presence.seconds", 1800.0, unit="Seconds")
    admission.assert_awaited_once()
    owner, transaction_id, event = admission.await_args.args
    assert owner == "org_a" and transaction_id == f"meeting:{ROOM}:1000"
    assert event["seconds"] == 1800 and event["kind"] == "meeting"
    allowance.assert_awaited_once()
    assert allowance.await_args.args[:2] == ("org_a", 1800)
    metronome.ingest.assert_not_awaited()

    # Speech: a 180 s agent answer in the meeting room closes like any Live call. On paid credits
    # that is the existing Metronome path -- the same mock the presence path above never touched.
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name in ("containers-t", "fast-gate-t"):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        containers = ddb.Table("containers-t")
        monkeypatch.setattr(vc, "_table", lambda: containers)
        monkeypatch.setattr(vc, "_fast_gate_table_name", lambda: "fast-gate-t")
        monkeypatch.setattr(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2")
        ddb.Table("fast-gate-t").put_item(
            Item={"owner_id": "org_a", "remaining_microcents": 1_000_000, "openrouter_reserved_microcents": 36_000}
        )
        call = {
            "owner_id": "__call__#c1",
            "callId": "c1",
            "ownerId": "org_a",
            "kind": "huddle",
            "roomId": ROOM,
            "startedAt": 1000,
            "deadline": 1600,
            "billing": "credits",
            "holdMicrocents": 36_000,
            "status": vc.OPEN,
        }
        containers.put_item(Item=call)
        assert await vc.close_call("c1", now=1300, usage_seconds=180)
    metronome.ingest.assert_awaited_once()
    (event,) = metronome.ingest.await_args.args[0]
    assert event["transaction_id"] == "c1" and event["properties"]["seconds"] == "180"


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
