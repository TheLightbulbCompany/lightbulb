"""Bridge task mode (spec 02 §3.3, lane V2): a task VM reads the specialist profile layered with
its overlay, writes only the overlay, writes the base manifest and the seal, never the root
auth.json, never runs the curator, and refuses every other kind of work. Without a latch nothing
changes (the last test)."""
import hashlib
import json
import os

import pytest

from bridge import server, sync, task_mode, turn
from tests.test_paperclip_run_record import FakeS3

AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
IDENTITY = {"runId": "0b5c6c39-6a43-5b7e-9d1a-3f0f2d7c1a11", "ownerId": "org_test", "taskId": TASK, "attempt": 2,
            "agentId": AGENT, "sessionKey": f"agent:{AGENT}:lightbulb:task:{TASK}", "mode": "worker",
            "session": {"kind": "task"}}
SPEC = f"{sync.S3_PREFIX}/{AGENT}/"
OVERLAY = f"org_test/hermes-tasks/{TASK}/{AGENT}/"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(autouse=True)
def _no_task(monkeypatch):
    monkeypatch.setattr(task_mode, "TASK", None)
    sync._restored.discard(AGENT)
    sync._ETAGS.pop(AGENT, None)


class LoggingS3(FakeS3):
    """FakeS3 plus the order of every write, with its key."""

    def __init__(self):
        super().__init__()
        self.writes: list[tuple[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, **kw):
        self.writes.append(("put", Key))
        return super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kw)

    def upload_file(self, src, bucket, key):
        self.writes.append(("upload", key))
        super().upload_file(src, bucket, key)

    def delete_objects(self, *, Bucket, Delete):
        self.writes.extend(("delete", o["Key"]) for o in Delete["Objects"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete["Objects"]], "Errors": []}


@pytest.fixture
def s3(monkeypatch):
    fake = LoggingS3()
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_s3", lambda: fake)
    return fake


def _seed(s3: FakeS3, **objects: bytes):
    for key, body in objects.items():
        s3.objects[key] = (body, 1.0)


def _profile(*files: str):
    p = sync._profile_dir(AGENT)
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.yaml").write_text("x")  # _propagate_deletes needs a real profile
    for rel in files:
        (p / rel).parent.mkdir(parents=True, exist_ok=True)
        (p / rel).write_text(f"local {rel}")
    return p


def test_task_mode_writes_only_task_prefix(home, s3):
    task_mode.latch(IDENTITY)
    _profile("memories/MEMORY.md", "skills/alpha/SKILL.md")
    sync.save_profile(AGENT)
    sync.put_paperclip_run_record(AGENT, "run-1", {"status": "completed"}, prune=False, runs_dir=sync.TASK_RUNS_DIR)
    # A file this process restored from the specialist and the agent then deleted locally
    sync._ETAGS.setdefault(AGENT, {})["memories/USER.md"] = '"e1"'
    sync._MTIMES.setdefault(AGENT, {})["memories/USER.md"] = 1
    sync.save_profile(AGENT)
    assert s3.writes, "nothing was written"
    assert all(key.startswith(OVERLAY) for _, key in s3.writes), s3.writes
    assert ("delete", OVERLAY + "memories/USER.md") in s3.writes  # never the specialist's copy
    assert not [k for _, k in s3.writes if k.startswith(SPEC)]


def test_restore_layers_specialist_then_task_and_skips_state_db_and_work(home, s3):
    task_mode.latch(IDENTITY)
    _seed(s3, **{
        SPEC + "memories/MEMORY.md": b"v1", SPEC + "state.db": b"spec-db", SPEC + "state.db-wal": b"w",
        SPEC + "work/x": b"work", SPEC + "skills/alpha/SKILL.md": b"alpha", SPEC + "config.yaml": b"c",
        OVERLAY + "memories/MEMORY.md": b"v2", OVERLAY + "state.db": b"task-db",
    })
    sync.restore_profile(AGENT)
    p = sync._profile_dir(AGENT)
    assert (p / "memories/MEMORY.md").read_bytes() == b"v2"  # the overlay wins
    assert (p / "state.db").read_bytes() == b"task-db"  # the task's own, from an earlier attempt
    assert not (p / "state.db-wal").exists() and not (p / "work/x").exists()  # the specialist's are skipped
    assert (p / "skills/alpha/SKILL.md").read_bytes() == b"alpha" and (p / "config.yaml").exists()
    assert sync.read_prefixes(AGENT) == [SPEC, OVERLAY] and sync.write_prefix(AGENT) == OVERLAY


def test_base_manifest_written_once_at_first_restore(home, s3):
    task_mode.latch(IDENTITY)
    _seed(s3, **{
        SPEC + "memories/MEMORY.md": b"entry one", SPEC + "skills/alpha/SKILL.md": b"alpha",
        SPEC + "skills/workspace/w/SKILL.md": b"workspace skill", SPEC + "skills/instagram/credentials.json": b"{}",
        SPEC + "skills/.usage.json": b'{"alpha": {"use_count": 1}}', SPEC + "config.yaml": b"c",
        OVERLAY + "memories/USER.md": b"user note",
    })
    sync.restore_profile(AGENT)
    assert sync.write_base_manifest(AGENT, 2) is True
    key = OVERLAY + "base-0002.json"
    base = json.loads(s3.objects[key][0])
    assert base["attempt"] == 2
    assert set(base["files"]) == {"memories/MEMORY.md", "memories/USER.md", "skills/.usage.json", "skills/alpha/SKILL.md"}
    assert base["files"]["memories/MEMORY.md"] == {"sha256": _sha(b"entry one"), "etag": "", "text": "entry one"}
    assert base["files"]["memories/USER.md"]["text"] == "user note"
    assert "text" not in base["files"]["skills/alpha/SKILL.md"]
    assert sync.write_base_manifest(AGENT, 2) is False  # a retried start: once per attempt
    assert [k for _, k in s3.writes if k == key] == [key]
    assert sync.write_base_manifest(AGENT, 3) is True  # the next attempt, its own base


def test_seal_written_after_final_save_with_hashes(home, s3):
    task_mode.latch(IDENTITY)
    p = _profile("memories/MEMORY.md", "skills/alpha/SKILL.md", "skills/alpha/ref.md", "state.db")
    sync.save_profile(AGENT)
    sync.seal_attempt(AGENT, TASK, 2)
    assert s3.writes[-1] == ("put", OVERLAY + "sealed.json")  # the last S3 call of the attempt
    seal = json.loads(s3.objects[OVERLAY + "sealed.json"][0])
    assert seal["attempt"] == 2 and seal["taskId"] == TASK
    assert seal["files"] == {
        rel: _sha((p / rel).read_bytes()) for rel in ("memories/MEMORY.md", "skills/alpha/SKILL.md", "skills/alpha/ref.md")
    }


def test_task_mode_refuses_room_ingress_and_other_agents(home, monkeypatch):
    task_mode.latch(IDENTITY)
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: True)
    auth = {"authorization": "Bearer gw-test"}
    inbound = server.handle_plugin(f"/plugins/lightbulb-channel/inbound/{AGENT}", "POST", auth, {"targetAgentId": AGENT})
    assert inbound == (409, {"error": "task_mode"})
    assert server.handle_plugin("/plugins/lightbulb-channel/paperclip/runs", "POST", auth, {}) == (409, {"error": "task_mode"})
    assert server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", auth, {"method": "health", "params": {}})[0] == 200
    with pytest.raises(task_mode.Refused):
        task_mode.check_agent("other-agent")
    with pytest.raises(task_mode.Refused):
        task_mode.latch({**IDENTITY, "taskId": "11111111-2222-4333-8444-555555555555"})
    with pytest.raises(task_mode.Refused):
        sync.write_prefix("other-agent")
    task_mode.latch({**IDENTITY, "attempt": 3})  # the same task's next attempt re-affirms the latch
    assert task_mode.active()["attempt"] == 3


def test_task_mode_never_saves_root_auth(home, monkeypatch):
    """The exact state in which the workspace VM DOES write the rotated grant back (restored at
    revision r / ETag e, unchanged since, local file rotated): a task VM makes no S3 call."""
    monkeypatch.setattr(sync, "S3_BUCKET", "bucket-under-test")
    monkeypatch.setattr(sync, "_ROOT_AUTH_HASH", None)
    monkeypatch.setattr(sync, "_ROOT_AUTH_REVISION", "r")
    monkeypatch.setattr(sync, "_ROOT_AUTH_ETAG", '"e"', raising=False)
    monkeypatch.setattr(sync, "_root_auth_revision", lambda: "r")
    monkeypatch.setattr(sync, "_root_auth_etag", lambda: '"e"')
    (home / "auth.json").write_text('{"providers": {"openai-codex": {"tokens": {"refresh_token": "rotated"}}}}')

    def touched(*a, **k):
        raise AssertionError("S3 touched")

    monkeypatch.setattr(sync, "_upload_object", touched)
    monkeypatch.setattr(sync, "_s3", touched)
    task_mode.latch(IDENTITY)
    sync.save_root_auth()  # a changed local grant on a task VM: no S3 call at all
    sync.save_rotated_cli_credentials()
    monkeypatch.setattr(task_mode, "TASK", None)
    with pytest.raises(AssertionError, match="S3 touched"):
        sync.save_root_auth()  # the workspace VM still writes it back (today's path)


def test_curator_skipped_in_task_mode(home, monkeypatch):
    import sys
    import types

    curator = types.SimpleNamespace(should_run_now=lambda: (_ for _ in ()).throw(AssertionError("curator consulted")))
    agent_pkg = types.ModuleType("agent")
    agent_pkg.curator = curator
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.curator", curator)
    monkeypatch.setattr(turn, "RESTORED", {AGENT})
    monkeypatch.setattr(turn, "save_profile", lambda agent_id: (_ for _ in ()).throw(AssertionError("saved")))
    turn._busy = 1
    task_mode.latch(IDENTITY)
    turn._curate_when_idle()  # returns before the curator is even imported
    monkeypatch.setattr(task_mode, "TASK", None)
    from bridge import followups

    monkeypatch.setattr(turn, "_hermes_home_scope", lambda agent_id: __import__("contextlib").nullcontext())
    monkeypatch.setattr(followups, "busy", lambda: False)
    with pytest.raises(AssertionError, match="curator consulted"):
        turn._curate_when_idle()  # the workspace VM asks the curator (today's path)


def test_workspace_mode_unchanged(home, s3):
    assert task_mode.active() is None
    assert sync.read_prefixes(AGENT) == [SPEC] and sync.write_prefix(AGENT) == SPEC
    assert sync._run_records_prefix(AGENT) == SPEC + sync.PAPERCLIP_RUNS_DIR + "/"
    assert sync.write_base_manifest(AGENT, 1) is False
    sync.seal_attempt(AGENT, TASK, 1)
    _profile("memories/MEMORY.md")
    sync.save_profile(AGENT)
    assert s3.writes == [("upload", SPEC + "memories/MEMORY.md")]


def test_ensure_profile_never_repulls_specialist_over_overlay(home, s3):
    """#4769 review: the specialist's .revision moves (another task's merge) while this task runs;
    the recency walk must not bring the specialist's copy down over the overlay-layered file."""
    task_mode.latch(IDENTITY)
    _seed(s3, **{SPEC + "memories/MEMORY.md": b"v1", SPEC + ".revision": b"r1", SPEC + "config.yaml": b"c",
                 OVERLAY + "memories/MEMORY.md": b"v2"})
    sync.restore_profile(AGENT)
    p = sync._profile_dir(AGENT)
    assert (p / "memories/MEMORY.md").read_bytes() == b"v2"
    s3.objects[SPEC + "memories/MEMORY.md"] = (b"v1-merged", 2.0)  # newer than the overlay's copy
    s3.objects[SPEC + ".revision"] = (b"r2", 2.0)
    sync.ensure_profile(AGENT)
    assert (p / "memories/MEMORY.md").read_bytes() == b"v2"
    assert not [k for _, k in s3.writes if k.startswith(SPEC)]


def test_restore_subtree_and_deferred_read_the_task_overlay(home, s3, monkeypatch):
    """#4769 review: a cold attempt 2+ gets the task's own work/<task> (and deferred trees) back
    from the overlay; the specialist's work/ is the workspace VM's and never comes down."""
    task_mode.latch(IDENTITY)
    monkeypatch.setattr(sync, "_start_deferred_restore", lambda agent_id: None)
    monkeypatch.setattr(sync, "_DEFERRED_RESTORE_DELAY_S", 0)
    _seed(s3, **{
        SPEC + f"work/{TASK}/notes.md": b"spec copy", SPEC + f"work/{TASK}/old.md": b"spec only",
        OVERLAY + f"work/{TASK}/notes.md": b"task copy", OVERLAY + f"work/{TASK}/new.md": b"task only",
        SPEC + ".curator_backups/x/a": b"spec backup", OVERLAY + "cache/images/b": b"task media",
    })
    sync.restore_profile(AGENT)
    p = sync._profile_dir(AGENT)
    assert sync.restore_subtree(AGENT, p / "work" / TASK) == 2
    assert (p / f"work/{TASK}/notes.md").read_bytes() == b"task copy" and (p / f"work/{TASK}/new.md").exists()
    assert not (p / f"work/{TASK}/old.md").exists()
    assert sync._restore_deferred(AGENT) == 2
    assert (p / ".curator_backups/x/a").exists() and (p / "cache/images/b").read_bytes() == b"task media"


def test_reseal_after_a_late_save_keeps_the_same_attempt(home, s3):
    task_mode.latch(IDENTITY)
    p = _profile("memories/MEMORY.md")
    sync.save_profile(AGENT)
    sync.seal_attempt(AGENT, TASK, 2)
    (p / "memories/MEMORY.md").write_text("a late write")  # after the final save; the SIGTERM save carries it
    sync.save_profile(AGENT)
    sync.reseal_attempt(AGENT)
    seal = json.loads(s3.objects[OVERLAY + "sealed.json"][0])
    assert seal["attempt"] == 2 and seal["files"]["memories/MEMORY.md"] == _sha(b"a late write")
    task_mode.latch({**IDENTITY, "attempt": 3})  # attempt 3 is running: its unfinished files are never sealed as 2's
    (p / "memories/MEMORY.md").write_text("attempt 3, unfinished")
    sync.save_profile(AGENT)
    sync.reseal_attempt(AGENT)
    assert json.loads(s3.objects[OVERLAY + "sealed.json"][0])["files"]["memories/MEMORY.md"] == _sha(b"a late write")


def test_sigterm_save_reseals_a_task_vm(home, s3, monkeypatch):
    task_mode.latch(IDENTITY)
    _profile("memories/MEMORY.md")
    monkeypatch.setattr(turn, "RESTORED", {AGENT})
    sync.seal_attempt(AGENT, TASK, 2)
    monkeypatch.setattr(server.network, "stop", lambda: None)
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        server._save_on_sigterm(15, None)
    assert s3.writes[-1] == ("put", OVERLAY + "sealed.json")  # the late save, then the seal again


def test_task_mode_refuses_network_rpc(home, monkeypatch):
    task_mode.latch(IDENTITY)
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: (_ for _ in ()).throw(AssertionError("member started")))
    auth = {"authorization": "Bearer gw-test"}
    for method in ("network.team", "memory.list"):
        code, reply = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", auth, {"method": method, "params": {}})
        assert (code, reply["ok"], reply["error"]["code"]) == (200, False, "task_mode"), method


def test_bad_identity_on_a_run_create_is_400(home, monkeypatch):
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: True)
    auth = {"authorization": "Bearer gw-test"}
    assert server.handle_plugin("/internal/v1/runs", "POST", auth, {"identity": "nope"}) == (400, {"error": "invalid_run_identity"})
    assert server.handle_plugin("/internal/v1/runs", "POST", auth, {"identity": {"session": "task"}})[0] == 400
