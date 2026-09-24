"""A native Paperclip run's terminal result must outlive the process that ran it.

Observed in dev 2026-09-24: a roll stopped the microVM in the same second the turn ended, the
adapter's next poll landed on a fresh process with an empty RUNS registry, the backend facade
mapped the 404 to `runtime_lost`, and Paperclip recorded a SUCCESSFUL run as failed. The fix
is a small terminal record under the agent's own profile prefix, written before the in-memory
flip and served on a registry miss.
"""
import datetime as dt
import io
import json
import time

import pytest

from bridge import sync, turn

RUN = "run-123"
SESSION = "paperclip:company:company-1:agent:agent-1:issue:issue-1"
IDENTITY = {
    "runId": RUN,
    "companyId": "company-1",
    "agentId": "agent-1",
    "sessionId": SESSION,
    "issueId": "issue-1",
    "paperclipApiUrl": "http://paperclip.internal:3100",
}
REQUEST = {"input": "do it", "instructions": "native", "session_id": SESSION}
USAGE = {**turn.ZERO_USAGE, "model": "m", "inputTokens": 7, "outputTokens": 3}


class Missing(Exception):
    def __init__(self, key):
        super().__init__(key)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, float]] = {}

    def put_object(self, *, Bucket, Key, Body, **_kw):
        self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), time.time())
        return {"ETag": "e"}

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise Missing(Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def upload_file(self, src, bucket, key):
        from pathlib import Path
        self.objects[key] = (Path(src).read_bytes(), time.time())

    def download_file(self, bucket, key, dest):
        from pathlib import Path
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.objects[key][0])

    def delete_objects(self, *, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.objects.pop(obj["Key"], None)

    def get_paginator(self, name):
        outer = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [
                    {"Key": k, "LastModified": dt.datetime.fromtimestamp(when, dt.timezone.utc)}
                    for k, (_b, when) in sorted(outer.objects.items()) if k.startswith(Prefix)
                ]}]

        return Paginator()


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    sync._restored.discard("agent-1")
    return fake


def _key(run_id=RUN):
    return f"{sync.S3_PREFIX}/agent-1/.paperclip-runs/{run_id}.json"


def _finish(outcome="completed", output="all done", error=None):
    turn.register_paperclip_run(IDENTITY, REQUEST)
    turn.finish_paperclip_run(RUN, outcome, output=output, error=error, usage=USAGE)


def test_finishing_a_run_writes_its_terminal_record(home, s3):
    _finish()
    record = json.loads(s3.objects[_key()][0])
    assert record["identity"]["runId"] == RUN
    assert record["outcome"] == "completed"
    assert record["output"] == "all done"


def test_the_record_is_written_before_the_in_memory_flip(home, s3, monkeypatch):
    seen = {}
    real = sync.put_paperclip_run_record

    def spy(agent_id, run_id, record):
        seen["state"] = turn.RUNS[run_id]["state"]
        real(agent_id, run_id, record)

    monkeypatch.setattr(sync, "put_paperclip_run_record", spy)
    _finish()
    assert seen["state"] != "ended"


def test_a_fresh_process_answers_from_the_record(home, s3):
    _finish()
    in_memory = turn.paperclip_run_status(RUN, IDENTITY)
    turn.RUNS.clear()  # the microVM was stopped; a new process has an empty registry
    assert turn.paperclip_run_status(RUN, IDENTITY) == in_memory
    assert in_memory["status"] == "completed" and in_memory["output"] == "all done"


def test_a_failed_run_record_keeps_its_error(home, s3):
    _finish("failed", output="", error="RuntimeError")
    turn.RUNS.clear()
    status = turn.paperclip_run_status(RUN, IDENTITY)
    assert status["status"] == "failed" and status["error"] == "RuntimeError"


def test_a_record_refuses_a_different_identity(home, s3):
    _finish()
    turn.RUNS.clear()
    assert turn.paperclip_run_status(RUN, {**IDENTITY, "issueId": "issue-2"}) is None


def test_no_record_and_no_entry_is_still_not_found(home, s3):
    assert turn.paperclip_run_status("never-ran", {**IDENTITY, "runId": "never-ran"}) is None


def test_a_record_write_failure_does_not_fail_the_turn(home, s3, monkeypatch, caplog):
    def boom(**_kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr(s3, "put_object", boom)
    _finish()
    assert turn.paperclip_run_status(RUN, IDENTITY)["status"] == "completed"
    assert "paperclip run record" in caplog.text


def test_a_profile_save_and_restore_leave_the_record_alone(home, s3):
    _finish()
    before = s3.objects[_key()]
    sync.restore_profile("agent-1")  # cold start: the record is not pulled into the profile
    assert not (sync._profile_dir("agent-1") / ".paperclip-runs").exists()
    stray = sync._profile_dir("agent-1") / ".paperclip-runs" / f"{RUN}.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}")  # even a local file of that name is never pushed over it
    sync.save_profile("agent-1")
    sync._pull_changed_objects("agent-1")
    assert s3.objects[_key()] == before
    assert json.loads(stray.read_text()) == {}


def test_a_record_write_prunes_this_agents_records_older_than_a_day(home, s3):
    old = time.time() - sync.PAPERCLIP_RUN_RECORD_TTL_S - 60
    s3.objects[_key("old-run")] = (b"{}", old)
    s3.objects[_key("recent-run")] = (b"{}", time.time() - 60)
    s3.objects[f"{sync.S3_PREFIX}/agent-1/memories/MEMORY.md"] = (b"keep", old)
    _finish()
    assert _key("old-run") not in s3.objects
    assert _key("recent-run") in s3.objects
    assert _key() in s3.objects
    assert f"{sync.S3_PREFIX}/agent-1/memories/MEMORY.md" in s3.objects
