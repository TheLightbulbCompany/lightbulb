"""Meeting transcripts in the product's own store: `<owner>/voice/transcripts/<room>/<id>.txt`."""

import uuid
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from core.config import settings
from core.services import hermes_profiles
from core.voice.meetings import transcripts

BUCKET = "test-replicas"
ROOM = str(uuid.uuid4())


@pytest.fixture
def s3(monkeypatch):
    import boto3
    from moto import mock_aws

    monkeypatch.setattr(hermes_profiles, "_SHARED_S3_CLIENT", None)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        with patch.object(settings, "OWNER_STATE_BUCKET", BUCKET):
            yield client


def _keys(s3) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def test_transcript_round_trip(s3):
    transcript_id = transcripts.put("org_a", ROOM, b"[10:00] Pat: hi\n", name="m|1")
    assert _keys(s3) == [f"org_a/voice/transcripts/{ROOM}/{transcript_id}.txt"]
    assert transcripts.get("org_a", ROOM, transcript_id) == b"[10:00] Pat: hi\n"
    assert transcripts.size("org_a", ROOM, transcript_id) == 16
    assert transcripts.put("org_a", ROOM, b"[10:00] Pat: hi!\n", name="m|1") == transcript_id  # a retry
    assert len(_keys(s3)) == 1 and transcripts.size("org_a", ROOM, transcript_id) == 17


def test_get_missing_is_gone(s3):
    assert transcripts.get("org_a", ROOM, "not-a-uuid") is None  # a non-UUID id
    assert transcripts.size("org_a", ROOM, str(uuid.uuid4())) is None


def test_get_failed_read_is_unreadable(s3, monkeypatch):
    transcript_id = transcripts.put("org_a", ROOM, b"hi\n", name="m|1")

    def denied(*_a, **_k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")

    monkeypatch.setattr(hermes_profiles._shared_s3_client(), "get_object", denied)
    with pytest.raises(ClientError):
        transcripts.get("org_a", ROOM, transcript_id)


def test_one_owner_cannot_read_anothers_transcript(s3):
    transcript_id = transcripts.put("org_a", ROOM, b"secret\n", name="m|1")
    assert transcripts.get("org_b", ROOM, transcript_id) is None


def test_oversized_transcript_is_bounded(s3):
    with pytest.raises(ValueError):
        transcripts.put("org_a", ROOM, b"x" * (transcripts.MAX_BYTES + 1), name="m|1")
    assert _keys(s3) == []
    transcripts.put("org_a", ROOM, b"x" * transcripts.MAX_BYTES, name="m|1")
