"""The exact items each product store function writes, and that the product's key prefixes and the engine's never
meet."""

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.config import settings
from core.voice import store

OWNER = "org_1"
NOW = 1_000_000.0


@pytest.fixture
def table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName="test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch.object(settings, "MESSAGING_TABLE", "test-messaging"),
            patch("time.time", return_value=NOW),
        ):
            yield t


def _rows(table) -> list[dict]:
    return sorted(table.scan()["Items"], key=lambda r: (r["pk"], r["sk"]))


def test_key_builders():
    assert store._voice_run_sk("r1") == "VOICERUN#r1"
    assert store._meeting_notes_sk("m1") == "MEETNOTES#m1"
    assert store.meeting_room_id(OWNER, "uid-1") == str(uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb-meeting|org_1|uid-1"))
    assert store.meeting_room_id(OWNER, "uid-1") == "9d830369-06b9-5d16-8d23-319e332ed77b"
    assert (store._VOICE_RUN_TTL_S, store._MEETING_NOTES_TTL_S) == (7200, 172800)


async def test_voice_run(table):
    row = await store.put_voice_run(OWNER, "r1", {"roomId": "room", "agentId": "a1"})
    expected = {"pk": "OWNER#org_1", "sk": "VOICERUN#r1", "runId": "r1", "roomId": "room", "agentId": "a1",
                "expiresAt": 1_007_200}  # fmt: skip
    assert _rows(table) == [expected]
    assert row == expected
    await store.put_voice_run(OWNER, "r1", {"spoken": "first"}, once=frozenset({"spoken"}))
    await store.put_voice_run(OWNER, "r1", {"spoken": "second"}, once=frozenset({"spoken"}))
    assert _rows(table) == [{**expected, "spoken": "first"}]
    assert await store.get_voice_run(OWNER, "r1") == {**expected, "spoken": "first"}
    with patch("time.time", return_value=1_007_200.0):
        assert await store.get_voice_run(OWNER, "r1") is None
    assert await store.get_voice_run(OWNER, "missing") is None


async def test_count_voice_edit(table):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert [await store.count_voice_edit(OWNER, "a1", "rename", 2) for _ in range(3)] == [True, True, False]
    assert _rows(table) == [
        {"pk": "OWNER#org_1", "sk": f"VOICEEDIT#a1#rename#{day}", "edits": 2, "expiresAt": 1_172_800}
    ]


async def test_voice_slot(table):
    assert await store.claim_voice_slot(OWNER, "VOICECAP#0", "call-1", 60)
    assert await store.claim_voice_slot(OWNER, "VOICECAP#0", "call-1", 60)  # a replay
    assert not await store.claim_voice_slot(OWNER, "VOICECAP#0", "call-2", 60)
    assert _rows(table) == [{"pk": "OWNER#org_1", "sk": "VOICECAP#0", "holder": "call-1", "expiresAt": 1_000_060}]
    await store.release_voice_slot(OWNER, "VOICECAP#0", "call-2")  # not the holder: nothing happens
    assert len(_rows(table)) == 1
    with patch("time.time", return_value=1_000_061.0):
        assert await store.claim_voice_slot(OWNER, "VOICECAP#0", "call-2", 60)  # expired
    await store.release_voice_slot(OWNER, "VOICECAP#0", "call-2")
    assert _rows(table) == []


async def test_meeting_notes_request(table):
    await store.put_meeting_notes_request(OWNER, "m1", {"roomId": "room", "occurrence": "o1"})
    await store.put_meeting_notes_request(OWNER, "m1", {"roomId": "other"})  # first write wins
    expected = {"pk": "OWNER#org_1", "sk": "MEETNOTES#m1", "roomId": "room", "occurrence": "o1",
                "expiresAt": 1_172_800}  # fmt: skip
    assert _rows(table) == [expected]
    assert await store.get_meeting_notes_request(OWNER, "m1") == expected
    assert await store.claim_meeting_notes_mail(OWNER, "m1")
    assert not await store.claim_meeting_notes_mail(OWNER, "m1")
    assert not await store.claim_meeting_notes_mail(OWNER, "missing")
    await store.put_meeting_notes_request(OWNER, "m1", {"roomId": "room"})  # a retried dispatch keeps the claim
    assert not await store.claim_meeting_notes_mail(OWNER, "m1")
    assert _rows(table) == [{**expected, "mailAttemptedAt": 1_000_000}]
    with patch("time.time", return_value=1_172_800.0):
        assert await store.get_meeting_notes_request(OWNER, "m1") is None


# --- whose sort keys are whose ---------------------------------------------------------------------------------------

BACKEND = Path(__file__).resolve().parents[3]
PRODUCT_FILES = [
    *sorted((BACKEND / "core/voice").rglob("*.py")),
    *(BACKEND / f"routers/{m}.py" for m in "voice voice_phone voice_meet voice_owner huddles meetings".split()),
]
ENGINE_REPO = BACKEND / "core/repositories/messaging_repo.py"
#: The product's key prefixes, as its code spells them (`"VOICEBOOK#`, `f"PHONE#`, `"LINE"`).
_PRODUCT_KEY = re.compile(r"[\"']((?:VOICE|PHONE|CALLPUSH|MEETNOTES)[A-Z]*#?|LINE(?=[\"']))")
#: Product rows the engine repository keeps, each for a reason: `VOICEAGENT` because the engine reads its
#: `frontAgentId` (`task_conversation._front_agent`); `VOICEUSAGE#` because voice metering rides the engine's outbox
#: (`WORK#VOICEUSAGE#` -> Pipe -> FIFO) and is kept untouched.
ENGINE_KEEPS = {"VOICEAGENT", "VOICEUSAGE#"}


def _keys(path: Path, pattern: re.Pattern) -> set[str]:
    return {m.group(1) for m in pattern.finditer(path.read_text())}


def test_disjoint_sort_keys():
    product = set().union(*(_keys(f, _PRODUCT_KEY) for f in PRODUCT_FILES))
    assert {"VOICERUN#", "MEETNOTES#", "VOICEEDIT#", "PHONEPIN#", "VOICENUMBER", "LINE", "VOICEBOOK#"} <= product
    engine = _keys(ENGINE_REPO, re.compile(r"[\"']([A-Z]{3,}[A-Z_]*#?)"))  # every key prefix the engine spells
    assert {"META", "MSG#", "MEMBER#", "IDEMP#", "WORK#", "RUN#"} <= engine
    clashes = sorted((p, e) for p in product for e in engine - ENGINE_KEEPS if p.startswith(e) or e.startswith(p))
    assert clashes == []


def test_engine_repo_has_no_product_keys():
    assert _keys(ENGINE_REPO, re.compile(r"\b((?:VOICE|PHONE|CALLPUSH|MEETNOTES)[A-Z]*#?)")) == ENGINE_KEEPS
