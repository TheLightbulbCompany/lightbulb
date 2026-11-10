"""Meeting chat crosses a provider boundary once, fenced to the originating occurrence."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core.services import meeting_chat as mod


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="meeting-chat-receipts",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(mod.webhook_dedup, "_table", lambda: table)
        monkeypatch.setattr(mod.webhook_dedup, "_emit_dedup_metric", lambda *a: None)
        trigger = {
            "triggerId": "chat:t",
            "actorUserId": "maya",
            "ref": {"origin": "meeting", "roomId": "r", "messageId": "m", "meetingBotId": "b", "meetingInCallAt": 100},
        }
        meta = {"ownerId": "org_a", "meeting": {"status": "in_call", "recallBotId": "b", "inCallAt": 100}}
        monkeypatch.setattr(
            mod.tasks_repo, "get_trigger", AsyncMock(side_effect=lambda ws, tid: trigger if tid == "chat:t" else None)
        )
        meta["meeting"]["invitedBy"] = "maya"
        monkeypatch.setattr(mod.messaging_repo, "get_room", AsyncMock(return_value=meta))
        monkeypatch.setattr(
            mod.messaging_repo,
            "list_members",
            AsyncMock(return_value=[{"kind": "human", "role": "owner", "id": "maya"}]),
        )
        send, post, routed = AsyncMock(), AsyncMock(return_value="posted"), AsyncMock()
        monkeypatch.setattr(mod.meeting_bots, "say", send)
        monkeypatch.setattr(mod.task_conversation, "post_as_lightbulb", post)
        monkeypatch.setattr(mod.tasks_repo, "set_trigger_routed_to", routed)
        lock = asyncio.Lock()

        @asynccontextmanager
        async def lease(_room):
            async with lock:
                yield

        monkeypatch.setattr(mod.meeting_invites, "meeting_lease", lease)
        yield {"meta": meta, "trigger": trigger, "send": send, "post": post, "routed": routed, "table": table}


async def test_concurrent_retry_sends_once_and_records_context_only_delivery(world):
    first, again = await asyncio.gather(*(mod.send("org_a", ["chat:t"], "The link") for _ in range(2)))
    assert first["status"] == again["status"] == "sent"
    assert {first["replayed"], again["replayed"]} == {False, True}
    world["send"].assert_awaited_once_with("b", "The link")
    assert world["post"].await_args.kwargs["attrs"]["voiceDelivery"] == "context"
    assert world["routed"].await_args.args[2] == {"meetingChat": "posted"}


async def test_lost_provider_response_is_unknown_and_never_resent(world):
    world["send"].side_effect = mod.meeting_bots.RecallError("timeout")
    assert (await mod.send("org_a", ["chat:t"], "The link"))["status"] == "unknown"
    assert (await mod.send("org_a", ["chat:t"], "The link"))["status"] == "unknown"
    world["send"].assert_awaited_once()
    world["post"].assert_not_awaited()


async def test_provider_rejection_is_not_claimed_sent(world):
    world["send"].side_effect = mod.meeting_bots.RecallError("403")
    assert (await mod.send("org_a", ["chat:t"], "The link"))["status"] == "refused"
    world["post"].assert_not_awaited()


@pytest.mark.parametrize("change", ["workspace", "bot", "occurrence", "ended", "not_meeting", "missing_trigger"])
async def test_refuses_cross_scope_and_stale_occurrence_before_any_receipt_or_send(world, change):
    if change == "workspace":
        world["meta"]["ownerId"] = "org_other"
    elif change == "bot":
        world["meta"]["meeting"]["recallBotId"] = "new-bot"
    elif change == "occurrence":
        world["meta"]["meeting"]["inCallAt"] = 200
    elif change == "ended":
        world["meta"]["meeting"]["status"] = "ended"
    elif change == "not_meeting":
        world["trigger"]["ref"]["origin"] = "chat"
    else:
        world["trigger"]["ref"] = {}
    with pytest.raises(mod.NotCurrentMeeting):
        await mod.send("org_a", ["chat:t"], "The link")
    world["send"].assert_not_awaited()
    assert world["table"].scan()["Count"] == 0


async def test_revoked_inviter_and_oversized_url_cannot_send(world, monkeypatch):
    monkeypatch.setattr(mod.messaging_repo, "list_members", AsyncMock(return_value=[]))
    with pytest.raises(mod.NotCurrentMeeting, match="no_current_inviter"):
        await mod.send("org_a", ["chat:t"], "A link")
    with pytest.raises(ValueError, match="500"):
        await mod.send("org_a", ["chat:t"], "https://example.com/" + "a" * 500)
    world["send"].assert_not_awaited()


@pytest.fixture
def optout(world, monkeypatch):
    """The meeting's record knows who spoke trigger line `m`; the shared writer is the one #5134 built."""
    world["meta"]["meeting"]["askSpeakers"] = {"cm": "12"}  # keyed by the message's client id
    world["meta"]["meeting"]["roster"] = {"12": {"name": "Sam"}, "11": {"name": "Maya"}}
    monkeypatch.setattr(mod.messaging_repo, "get_message_by_id", AsyncMock(return_value={"clientMessageId": "cm"}))
    rec = AsyncMock(return_value={"notTranscribed": [{"id": "12", "name": "Sam"}]})
    monkeypatch.setattr("routers.voice_meet.record_transcription", rec)
    return rec


async def test_stop_transcribing_excludes_the_speaker_of_the_line(optout):
    out = await mod.stop_transcribing("org_a", ["chat:t"])
    assert out == {"notTranscribed": [{"id": "12", "name": "Sam"}]}
    assert optout.await_args.args[0] == "r" and optout.await_args.args[2:] == ("12", "Sam")


async def test_stop_transcribing_refuses_another_participant(optout):
    with pytest.raises(mod.NotCurrentMeeting, match="not_the_speaker"):
        await mod.stop_transcribing("org_a", ["chat:t"], "11")
    optout.assert_not_awaited()


async def test_stop_transcribing_needs_a_recorded_speaker(optout, world):
    world["meta"]["meeting"]["askSpeakers"] = {}
    with pytest.raises(mod.NotCurrentMeeting, match="not_the_speaker"):
        await mod.stop_transcribing("org_a", ["chat:t"])
    optout.assert_not_awaited()


async def test_stop_transcribing_refuses_a_previous_occurrences_trigger(optout, world):
    world["meta"]["meeting"].update(recallBotId="b2", inCallAt=200)  # the series advanced to a new live occurrence
    with pytest.raises(mod.NotCurrentMeeting, match="not_current_meeting"):
        await mod.stop_transcribing("org_a", ["chat:t"])
    optout.assert_not_awaited()


def test_advancing_the_series_clears_who_spoke():
    assert "askSpeakers" in mod.meeting_invites._CALL_KEYS and "askSpeakers" in mod.meeting_invites._OCCURRENCE_KEYS


async def test_stop_transcribing_is_for_a_live_meeting_only(optout, world):
    world["trigger"]["ref"] = {"roomId": "r", "messageId": "m"}  # a plain chat trigger, no meeting origin
    with pytest.raises(mod.NotCurrentMeeting, match="not_a_meeting"):
        await mod.stop_transcribing("org_a", ["chat:t"])
    world["trigger"]["ref"] = {"origin": "meeting", "roomId": "r", "messageId": "m"}
    world["meta"]["meeting"]["status"] = "ended"
    with pytest.raises(mod.NotCurrentMeeting, match="not_current_meeting"):
        await mod.stop_transcribing("org_a", ["chat:t"])
    optout.assert_not_awaited()
