"""The #4762 review's repros, against the real `merge_learning` over moto (tasks table + S3)."""

import hashlib
import json
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.repositories import learning_repo, tasks_repo
from core.services import learning_merge as lm
from core.temporal import learning

WS, SPEC, TASK = "org_z", "a1", "55555555-5555-4555-8555-555555555555"
BUCKET, SPEC_PREFIX = "test-owner-state", "org_z/hermes/a1/"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _usage(n: int) -> bytes:
    return json.dumps({"alpha": {"use_count": n, "state": "active"}}, indent=2, sort_keys=True).encode()


def _base(files: dict[str, bytes]) -> bytes:
    return json.dumps(
        {"files": {r: {"sha256": _sha(b), "etag": '"e"', "text": b.decode()} for r, b in files.items()}}
    ).encode()


@pytest.fixture
def world(monkeypatch):
    from core.config import settings

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=dynamodb.table_name("tasks"),
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
        monkeypatch.setattr(learning, "_s3_client", lambda: s3)
        monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value={"lane": "shared"}))
        yield s3


def _put(s3, key: str, body: bytes) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)


def _spec(s3, rel: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=SPEC_PREFIX + rel)["Body"].read()


def _overlay() -> str:
    return learning.overlay_prefix(WS, TASK, SPEC)


async def test_untouched_usage_is_not_recounted(world):
    """Blocker 1: a seal-listed file the task never touched must not be read as T."""
    s3, prefix = world, _overlay()
    base_usage, cur_usage = _usage(5), _usage(7)  # another merge bumped C since this task started
    _put(s3, SPEC_PREFIX + lm.USAGE, cur_usage)
    _put(s3, SPEC_PREFIX + lm.MEMORY, b"fact one")
    _put(s3, prefix + lm.base_rel(1), _base({lm.USAGE: base_usage, lm.MEMORY: b"fact one"}))
    mem = b"fact one" + lm.DELIM.encode() + b"learned"
    _put(s3, prefix + lm.MEMORY, mem)  # the task wrote only MEMORY.md
    seal = {"attempt": 1, "files": {lm.MEMORY: _sha(mem), lm.USAGE: _sha(base_usage)}}  # usage at B's sha
    _put(s3, prefix + lm.SEALED, json.dumps(seal).encode())
    report = await learning.merge_learning(WS, SPEC, TASK, 1)
    assert report["applied"] is True and report["added"]["memory"] == 1
    assert json.loads(_spec(s3, lm.USAGE))["alpha"]["use_count"] == 7  # not 9
    assert _spec(s3, lm.MEMORY).decode().split(lm.DELIM) == ["fact one", "learned"]


async def test_superseded_attempt_learning_carried_by_next(world):
    """Blocker 2: a superseded attempt is marked applied but applied nothing; attempt 2 merges
    against attempt 1's base so attempt 1's entry is not lost."""
    s3, prefix = world, _overlay()
    _put(s3, SPEC_PREFIX + lm.MEMORY, b"fact one")
    m1 = "fact one" + lm.DELIM + "E1"
    m2 = m1 + lm.DELIM + "E2"
    _put(s3, prefix + lm.base_rel(1), _base({lm.MEMORY: b"fact one"}))
    _put(s3, prefix + lm.base_rel(2), _base({lm.MEMORY: m1.encode()}))
    _put(s3, prefix + lm.MEMORY, m2.encode())
    _put(s3, prefix + lm.SEALED, json.dumps({"attempt": 2, "files": {lm.MEMORY: _sha(m2.encode())}}).encode())
    assert (await learning.merge_learning(WS, SPEC, TASK, 1))["skipped"] == "superseded"
    assert (await learning_repo.get(WS, SPEC, TASK, 1))["state"] == "applied"  # idempotent, but applied nothing
    assert (await learning.merge_learning(WS, SPEC, TASK, 2))["added"]["memory"] == 2
    assert _spec(s3, lm.MEMORY).decode().split(lm.DELIM) == ["fact one", "E1", "E2"]


async def test_sealed_file_gone_from_overlay_is_not_written_empty(world):
    """Blocker 3: a sealed skill file a later attempt deleted from the overlay (and C never had)
    is left out of T, never written as an empty SKILL.md."""
    s3, prefix = world, _overlay()
    _put(s3, SPEC_PREFIX + lm.MEMORY, b"fact one")
    _put(s3, prefix + lm.base_rel(1), _base({lm.MEMORY: b"fact one"}))
    _put(s3, prefix + lm.MEMORY, b"fact one")
    seal = {"attempt": 1, "files": {lm.MEMORY: _sha(b"fact one"), "skills/x/SKILL.md": _sha(b"# x")}}
    _put(s3, prefix + lm.SEALED, json.dumps(seal).encode())
    report = await learning.merge_learning(WS, SPEC, TASK, 1)
    assert report["added"]["skills"] == []
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=SPEC_PREFIX).get("Contents", [])}
    assert SPEC_PREFIX + "skills/x/SKILL.md" not in keys, sorted(keys)


async def test_missing_task_row_merges_nothing(world, monkeypatch):
    monkeypatch.setattr(tasks_repo, "get_task", AsyncMock(return_value=None))
    _put(world, _overlay() + lm.MEMORY, b"orphan")
    assert (await learning.merge_learning(WS, SPEC, TASK, 1))["skipped"] == "no_task"
    assert (await learning_repo.get(WS, SPEC, TASK, 1))["state"] == "applied"
    assert "Contents" not in world.list_objects_v2(Bucket=BUCKET, Prefix=SPEC_PREFIX)


async def test_end_attempt_hook_never_fails_end_attempt(monkeypatch, caplog):
    from core.services import runtime_transport

    monkeypatch.setattr(learning, "close_task_attempt", AsyncMock(side_effect=RuntimeError("temporal down")))
    att = {"agentId": SPEC, "sessionId": runtime_transport.session_id_for(WS, TASK)}
    await learning.close_task_attempt_if_task_session(WS, TASK, att, 1)  # logged, not raised
    assert "close_task_attempt failed" in caplog.text
