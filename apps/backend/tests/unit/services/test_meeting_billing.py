"""A meeting's presence is ours to pay (a metric, no Metronome); agent speech in it draws the
workspace's daily voice allowance exactly as Live does."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from core.services import voice_calls as vc  # noqa: E402
from routers import meetings  # noqa: E402

ROOM = "11111111-1111-4111-8111-111111111111"


async def test_presence_is_not_billed_and_speech_draws_the_allowance(monkeypatch):
    # Presence: in_call at t=1000, call_ended at t=2800 -> 1800 s on the metric, nothing to Metronome.
    meta = {"roomId": ROOM, "ownerId": "org_a", "meeting": {"status": "bot_created", "recallBotId": "bot-1"}}

    async def patch_meeting(room_id, fields, drop=()):
        meta["meeting"] = {**meta["meeting"], **fields}
        return meta["meeting"]

    clock = MagicMock(return_value=1000.0)
    metronome = MagicMock()
    monkeypatch.setattr(vc, "_metronome", metronome)
    with (
        patch.object(meetings.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta)),
        patch.object(meetings.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting)),
        patch.object(meetings.container_repo, "try_claim_pass", AsyncMock(return_value=True)),
        patch.object(meetings.clerk_admin, "get_organization", AsyncMock(return_value={})),
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
    metronome.assert_not_called()

    # Speech: a 180 s agent answer in the meeting room closes like any Live call, on the free counter.
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="containers-t",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        containers = ddb.Table("containers-t")
        monkeypatch.setattr(vc, "_table", lambda: containers)
        call = {
            "owner_id": "__call__#c1",
            "callId": "c1",
            "ownerId": "org_a",
            "kind": "huddle",
            "roomId": ROOM,
            "startedAt": 1000,
            "deadline": 1600,
            "billing": "free",
            "holdMicrocents": 0,
            "status": vc.OPEN,
        }
        containers.put_item(Item=call)
        assert await vc.close_call("c1", now=1300, usage_seconds=180)
        used = containers.get_item(Key={"owner_id": "__voice__#org_a"})["Item"]["usedSeconds"]
    assert int(used) == 180
    metronome.assert_not_called()


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
