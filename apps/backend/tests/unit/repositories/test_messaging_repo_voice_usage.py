"""Voice metering events and their stream-to-FIFO work pointer commit together."""

import pytest

from core.repositories import messaging_repo as repo
from tests.unit.repositories.test_messaging_repo import OWNER, table  # noqa: F401


@pytest.mark.asyncio
async def test_voice_event_and_fifo_pointer_are_atomic_and_replay_immutable(table):  # noqa: F811
    event = {
        "transactionId": "call-1",
        "customerId": OWNER,
        "timestamp": "2026-11-08T12:00:00Z",
        "seconds": 90,
        "kind": "phone",
        "properties": {"seconds": "90", "kind": "phone"},
    }
    row = await repo.put_voice_usage_event(OWNER, "call-1", event)
    assert row["voiceUsageEvent"] == event

    source = table.get_item(Key={"pk": repo.owner_pk(OWNER), "sk": "VOICEUSAGE#call-1"})["Item"]
    pointer = table.get_item(Key={"pk": repo.owner_pk(OWNER), "sk": "WORK#VOICEUSAGE#call-1"})["Item"]
    assert source["voiceUsageEvent"] == event
    assert pointer["payload"] == {"kind": "voice_usage", "ownerId": OWNER, "transactionId": "call-1"}
    assert pointer["messageGroupId"] == OWNER
    assert pointer["messageDeduplicationId"]
    assert pointer["expiresAt"] > 0

    changed = {**event, "seconds": 900, "timestamp": "2026-11-09T12:00:00Z"}
    replay = await repo.put_voice_usage_event(OWNER, "call-1", changed)
    assert replay["voiceUsageEvent"] == event
    assert table.scan()["Items"] == [source, pointer]


@pytest.mark.asyncio
async def test_voice_event_updates_existing_owner_settlement_receipt(table):  # noqa: F811
    key = {"pk": repo.owner_pk(OWNER), "sk": "VOICEUSAGE#owner:room:job"}
    table.put_item(Item={**key, "startedAt": "2026-11-08T12:00:00Z", "seconds": 90, "billing": "plan"})
    event = {"transactionId": "owner:room:job", "customerId": OWNER, "timestamp": "2026-11-08T12:00:00Z"}

    row = await repo.put_voice_usage_event(OWNER, "owner:room:job", event)
    saved = table.get_item(Key=key)["Item"]
    assert row["voiceUsageEvent"] == event
    assert saved["startedAt"] == "2026-11-08T12:00:00Z" and saved["seconds"] == 90 and saved["billing"] == "plan"
    assert table.get_item(Key={"pk": key["pk"], "sk": "WORK#VOICEUSAGE#owner:room:job"}).get("Item")
