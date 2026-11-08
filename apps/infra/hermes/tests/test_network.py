"""bridge.network: the `lightbulb member` sidecar's start, save and stop, against a fake engine.

The fake (written into tmp_path) speaks the line protocol: the token is stdin's first line, each
socket line is one JSON op, `subscribe` streams events, `snapshot` writes the file it is given,
`stop` (or SIGTERM) writes the final snapshot and exits 0. Its knobs live in cfg.json (read at
spawn); answers.json (re-read per op) is an op's `result` (or, with ok:false, its whole answer);
calls.jsonl logs each op without its token.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import bridge
from bridge import network, sync

KEY = base64.b64encode(bytes(range(32))).decode()
SESSIONS = {"workspace": "Acme", "sessions": [{"name": "friday", "agent": "Friday", "title": "Friday",
                                               "agentId": "a-friday"}]}

FAKE = r'''#!{python}
import json, os, signal, socket, sys, threading, time
from pathlib import Path
D = Path({dir!r})
args = sys.argv[1:]
opt = {{args[i]: args[i + 1] for i in range(1, len(args) - 1, 2)}}
with open(D / "spawns.log", "a") as fh:
    fh.write("spawn\n")
(D / "argv.json").write_text(json.dumps(args))
(D / "env.json").write_text(json.dumps(dict(os.environ)))
cfg = json.loads((D / "cfg.json").read_text()) if (D / "cfg.json").exists() else {{}}
if cfg.get("fail"):
    sys.exit(3)
token = sys.stdin.readline().strip()
subs = []
time.sleep(cfg.get("socket_delay", 0))

def snapshot(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes((D / "state.txt").read_bytes() if (D / "state.txt").exists() else b"v0")

def bye(*_):
    snapshot(opt["--snapshot"])
    os._exit(0)

signal.signal(signal.SIGTERM, bye)

def serve(conn):
    fh = conn.makefile("rb")
    for raw in fh:
        msg = json.loads(raw)
        with open(D / "ops.log", "a") as log:
            log.write(msg.get("op", "") + "\n")
        if msg.get("token") != token:
            conn.sendall((json.dumps({{"id": msg.get("id"), "ok": False, "error": "unauthorized"}}) + "\n").encode())
            conn.close()
            return
        op = msg["op"]
        if op == "subscribe":
            subs.append(conn)
            for ev in cfg.get("events", []):
                conn.sendall((json.dumps(ev) + "\n").encode())
            continue
        with open(D / "calls.jsonl", "a") as log:
            log.write(json.dumps({{k: v for k, v in msg.items() if k != "token"}}) + "\n")
        if op == "snapshot":
            snapshot(msg["path"])
        answers = json.loads((D / "answers.json").read_text()) if (D / "answers.json").exists() else {{}}
        got = answers.get(op, {{}})
        if got.get("ok") is False:   # a refusal replaces the envelope
            answer = {{**got, "id": msg["id"]}}
        else:
            answer = {{"id": msg["id"], "ok": True, "result": got}}
        conn.sendall((json.dumps(answer) + "\n").encode())
        if op == "stop":
            if cfg.get("hang_on_stop"):
                continue
            bye()

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(opt["--socket"])
srv.listen(16)
while True:
    c, _ = srv.accept()
    threading.Thread(target=serve, args=(c,), daemon=True).start()
'''


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts: list[dict] = []
        self.deletes: list[str] = []
        self.conflict = False
        self._n = 0

    def seed(self, key, data: bytes):
        self._n += 1
        self.objects[key] = (data, f'"e{self._n}"')

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _Err("NoSuchKey")
        data, etag = self.objects[Key]

        class _Body:
            def read(self_inner):
                return data
        return {"Body": _Body(), "ETag": etag}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _Err("404")
        return {"ETag": self.objects[Key][1]}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        self.puts.append({"Key": Key, "Body": Body, "IfMatch": IfMatch, "IfNoneMatch": IfNoneMatch})
        current = self.objects.get(Key)
        if self.conflict or (IfMatch and (not current or current[1] != IfMatch)) or (IfNoneMatch and current):
            raise _Err("PreconditionFailed")
        self.seed(Key, Body)
        return {"ETag": self.objects[Key][1]}

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop(Key, None)
        return {}


@pytest.fixture
def net(tmp_path, monkeypatch):
    sock_dir = Path(tempfile.mkdtemp(prefix="lbn"))  # AF_UNIX paths are capped near 104 bytes
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    exe = fake_dir / "lightbulb"
    exe.write_text(FAKE.format(python=sys.executable, dir=str(fake_dir)))
    exe.chmod(0o755)
    s3 = FakeS3()
    s3.seed("org_test/network/sessions.json", json.dumps(SESSIONS).encode())
    metrics: list[dict] = []
    monkeypatch.setattr(sync, "_s3", lambda: s3)
    monkeypatch.setattr(sync, "emit_metrics", lambda m: metrics.append(m))
    monkeypatch.setattr(bridge, "NETWORK_KEY", KEY)
    monkeypatch.setattr(bridge, "S3_BUCKET", "lightbulb-dev-openclaw-replicas")
    monkeypatch.setattr(bridge, "OWNER", "org_test")
    monkeypatch.setattr(network, "LIGHTBULB", str(exe))
    monkeypatch.setattr(network, "NET_HOME", tmp_path / "data" / "network")
    monkeypatch.setattr(network, "SNAPSHOT", tmp_path / "data" / "network-out" / "state.db")
    monkeypatch.setattr(network, "SESSIONS_FILE", tmp_path / "data" / "network-out" / "sessions.json")
    monkeypatch.setattr(network, "SOCKET", sock_dir / "n.sock")
    monkeypatch.setattr(network, "SAVE_DEBOUNCE_S", 0.05)
    for name, value in (("_proc", None), ("_token", ""), ("_etag", None), ("_sha", None), ("_next_try", 0.0),
                        ("_off", False), ("_stopping", False), ("_ready", False), ("_sessions", {}), ("_save_timer", None),
                        ("_skip_logged", False)):
        monkeypatch.setattr(network, name, value)
    network._superseded.clear()
    network.on_event.clear()

    class Net:
        pass
    n = Net()
    n.s3, n.dir, n.metrics, n.tmp = s3, fake_dir, metrics, tmp_path
    n.spawns = lambda: len((fake_dir / "spawns.log").read_text().splitlines()) if (fake_dir / "spawns.log").exists() else 0
    n.cfg = lambda **kw: (fake_dir / "cfg.json").write_text(json.dumps(kw))
    n.state = lambda b: (fake_dir / "state.txt").write_bytes(b)
    n.answer = lambda **ops: (fake_dir / "answers.json").write_text(json.dumps(ops))  # op name -> extra answer fields
    n.calls = lambda op=None: [c for c in (json.loads(x) for x in (fake_dir / "calls.jsonl").read_text().splitlines())
                               if op is None or c["op"] == op] if (fake_dir / "calls.jsonl").exists() else []
    yield n
    if network._proc is not None and network._proc.poll() is None:
        network._proc.kill()
        network._proc.wait()
    shutil.rmtree(sock_dir, ignore_errors=True)


def _until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _state_puts(n):
    return [p for p in n.s3.puts if p["Key"] == "org_test/network/state.db"]


def test_a_pooled_microvm_without_an_invoke_starts_no_engine(net):
    from bridge import server
    assert server._ping_status() in ("Healthy", "HealthyBusy")
    assert server.handle_plugin("/x", "POST", {"authorization": "Bearer wrong"}, {})[0] == 401
    time.sleep(0.2)
    assert net.spawns() == 0
    # The first authorized invoke is what starts it, in the background.
    assert server.handle_plugin("/x", "POST", {"authorization": f"Bearer {bridge.GATEWAY_TOKEN}"}, {})[0] == 404
    assert _until(lambda: net.spawns() == 1 and network._running())


def test_no_key_or_no_sessions_file_starts_nothing(net, monkeypatch):
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    assert network.ensure_started(background=False) is False
    monkeypatch.setattr(bridge, "NETWORK_KEY", KEY)
    del net.s3.objects["org_test/network/sessions.json"]
    assert network.ensure_started(background=False) is False
    assert net.spawns() == 0
    with pytest.raises(network.NetworkOff):
        network.call("directory")


def test_start_restores_state_db_and_writes_identity_key_0600(net):
    net.s3.seed("org_test/network/state.db", b"kept-state")
    assert network.ensure_started(background=False) is True
    home = network.NET_HOME
    assert (home / "state.db").read_bytes() == b"kept-state"
    key = home / "identity.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.read_bytes() == base64.b64decode(KEY)
    argv = json.loads((net.dir / "argv.json").read_text())
    assert argv == ["member", "--home", str(home), "--snapshot", str(network.SNAPSHOT), "--socket",
                    str(network.SOCKET), "--name", "Acme", "--sessions", str(network.SESSIONS_FILE)]
    assert network.session_for_agent("a-friday") == "friday"
    assert network.agent_for_session("friday") == "a-friday"
    assert network.call("directory")["ok"] is True
    assert "identity.key" not in " ".join(p["Key"] for p in net.s3.puts)


def test_restore_drops_a_stale_journal_first(net, monkeypatch):
    net.s3.seed("org_test/network/state.db", b"kept-state")
    home = network.NET_HOME
    home.mkdir(parents=True)
    for leftover in ("state.db-journal", "state.db-wal", "state.db-shm"):
        (home / leftover).write_bytes(b"hot journal from a crashed sidecar")
    seen = {}
    real_popen = network.subprocess.Popen

    def popen(*a, **kw):  # what the sidecar would open
        seen["files"] = sorted(p.name for p in home.iterdir())
        return real_popen(*a, **kw)

    monkeypatch.setattr(network.subprocess, "Popen", popen)
    assert network.ensure_started(background=False)
    assert seen["files"] == ["identity.key", "state.db"]
    assert (home / "state.db").read_bytes() == b"kept-state"


def test_an_rpc_during_a_background_start_waits_for_the_socket(net):
    """The 13:26Z race: the boot path starts the sidecar in the background, and an RPC arriving
    before its socket opened was told the network runs, then failed to connect."""
    net.cfg(socket_delay=1.0)
    network.ensure_started(background=True)
    assert _until(lambda: net.spawns() == 1)
    assert network.ensure_started(background=False)
    assert network.call("status", {})["ok"] is True
    assert net.spawns() == 1


def test_concurrent_first_invokes_start_one_sidecar(net):
    results = []
    threads = [threading.Thread(target=lambda: results.append(network.ensure_started(background=False)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results) and len(results) == 8
    assert net.spawns() == 1


def test_a_failed_start_retries_after_60_s_not_before(net, monkeypatch, caplog):
    net.cfg(fail=True)
    assert network.ensure_started(background=False) is False
    assert "network.start_failed" in caplog.text
    assert net.spawns() == 1
    assert 59 < network._next_try - time.monotonic() <= 60
    net.cfg()
    assert network.ensure_started(background=False) is False   # inside the 60 s: no try
    assert net.spawns() == 1
    monkeypatch.setattr(network, "_next_try", time.monotonic() - 0.001)  # the 60 s have passed
    assert network.ensure_started(background=False) is True
    assert net.spawns() == 2


def test_saves_are_debounced_skipped_when_unchanged_and_conditional(net):
    assert network.ensure_started(background=False)
    net.state(b"v1")
    for _ in range(5):
        network.call("send", {"from": "friday", "to": "mac", "text": "hi", "request_id": "r"})
    assert _until(lambda: len(_state_puts(net)) == 1)
    time.sleep(0.2)
    puts = _state_puts(net)
    assert len(puts) == 1 and puts[0]["IfNoneMatch"] == "*" and puts[0]["IfMatch"] is None
    first_etag = net.s3.objects["org_test/network/state.db"][1]
    network.call("directory")                            # same bytes -> snapshot, but no put
    time.sleep(0.3)
    assert len(_state_puts(net)) == 1
    net.state(b"v2")
    network.call("send", {"from": "friday", "to": "mac", "text": "again", "request_id": "r2"})
    assert _until(lambda: len(_state_puts(net)) == 2)
    assert _state_puts(net)[1]["IfMatch"] == first_etag


def test_a_412_stops_the_sidecar_and_keeps_the_network_off(net):
    net.s3.seed("org_test/network/state.db", b"old")
    assert network.ensure_started(background=False)
    proc = network._proc
    net.s3.conflict = True
    net.state(b"mine")
    network.call("send", {"from": "friday", "to": "mac", "text": "hi", "request_id": "r"})
    assert _until(lambda: proc.poll() is not None)
    assert {"network.state_conflict": (1, "Count")} in net.metrics
    assert network.ensure_started(background=False) is False
    assert net.spawns() == 1
    with pytest.raises(network.NetworkOff):
        network.call("directory")


def test_sigterm_runs_the_network_stop_beside_the_profile_saves(net, monkeypatch):
    from bridge import server, turn
    assert network.ensure_started(background=False)
    net.cfg(hang_on_stop=True)  # read at spawn only: restart so the knob applies
    network._proc.kill()
    network._proc.wait()
    assert network.ensure_started(background=False)
    started = {}
    exited = []
    real_stop = network.stop

    def slow_save(_agent):
        time.sleep(8)

    def timed_stop(*a, **kw):
        started["at"] = time.monotonic()
        real_stop(*a, **kw)
        started["done"] = time.monotonic()

    monkeypatch.setattr(turn, "RESTORED", {"a"})
    monkeypatch.setattr(sync, "save_profile", slow_save)
    monkeypatch.setattr(sync, "save_root_auth", lambda: None)
    monkeypatch.setattr(network, "stop", timed_stop)
    monkeypatch.setattr(server.os, "_exit", lambda code: exited.append(code))
    t0 = time.monotonic()
    server._save_on_sigterm(15, None)
    assert exited == [143]
    assert started["at"] - t0 < 0.5                      # not queued behind the slow save
    assert started["done"] - started["at"] <= 6.5        # a hung sidecar is killed at 6 s
    assert network._proc is None
    assert time.monotonic() - t0 <= server.SIGTERM_SAVE_S + 0.5


def test_stop_puts_the_final_snapshot(net):
    assert network.ensure_started(background=False)
    net.state(b"final")
    network.stop()
    assert net.s3.objects["org_test/network/state.db"][0] == b"final"


def test_four_superseded_in_ten_minutes_emit_member_key_contested(net):
    ev = {"event": "superseded", "by_peer": "abc"}
    for _ in range(3):
        network._handle_event(ev)
    assert not net.metrics
    aged = [t - 601 for t in network._superseded]                 # those three are now > 10 min old
    network._superseded.clear()
    network._superseded.extend(aged)
    network._handle_event(ev)
    assert not net.metrics
    for _ in range(3):
        network._handle_event(ev)
    assert net.metrics == [{"network.member_key_contested": (1, "Count")}]


def test_the_token_never_reaches_os_environ_or_a_file(net):
    seen = []
    network.on_event.append(seen.append)
    net.cfg(events=[{"event": "message", "session": "friday"}])
    assert network.ensure_started(background=False)
    token = network._token
    assert len(token) >= 32
    assert _until(lambda: seen == [{"event": "message", "session": "friday"}])
    network.call("directory")
    network.stop()
    assert all(token not in v for v in os.environ.values())
    assert token not in (net.dir / "env.json").read_text()
    for path in net.tmp.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


def test_a_save_after_reconnect_deleted_the_prefix_puts_nothing(net, caplog):
    assert network.ensure_started(background=False)
    del net.s3.objects["org_test/network/sessions.json"]   # the backend's "Reconnect workspace"
    net.state(b"old-key-state")
    network.call("send", {"from": "friday", "to": "mac", "text": "hi", "request_id": "r"})
    time.sleep(0.3)
    network.stop()                                          # the SIGTERM save too
    assert _state_puts(net) == []
    assert caplog.text.count("network.save_skipped_reconnect") == 1


def test_a_save_that_a_reconnect_overtakes_mid_put_is_taken_back(net, caplog):
    """The prefix vanishes between the save's check and its put: the put lands, the re-check
    sees the reconnect, and the write is deleted again (Codex P2 on #4325)."""
    assert network.ensure_started(background=False)
    real_put = net.s3.put_object

    def put_then_reconnect(Bucket, Key, Body, **cond):
        resp = real_put(Bucket, Key, Body, **cond)
        if Key.endswith("/state.db"):
            net.s3.objects.pop("org_test/network/sessions.json", None)  # reconnect lands "during" the put
        return resp
    net.s3.put_object = put_then_reconnect
    net.state(b"old-key-state")
    network.call("send", {"from": "friday", "to": "mac", "text": "hi", "request_id": "r"})
    assert _until(lambda: "org_test/network/state.db" in net.s3.deletes)
    assert len(_state_puts(net)) == 1
    assert "org_test/network/state.db" not in net.s3.objects
    assert _until(lambda: not network.running())
    assert caplog.text.count("network.save_skipped_reconnect") == 1
