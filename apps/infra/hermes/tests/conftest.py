import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("GATEWAY_TOKEN", "gw-test")
os.environ.setdefault("ISOL8_OWNER_ID", "org_test")
os.environ.setdefault("ISOL8_API_URL", "https://api-dev.example.invalid")
os.environ.setdefault("OPENCLAW_HOOK_TOKEN", "hook-test")
os.environ.setdefault("S3_BUCKET", "")          # sync is a no-op without a bucket


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME, re-pointed on the already-imported modules."""
    from bridge import sync
    (tmp_path / "profiles").mkdir(parents=True)
    monkeypatch.setattr(sync, "HOME", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_sync_state():
    """M6-R51's per-agent maps are keyed by agent id, not by HOME, so a previous test's
    tmp_path stamps would otherwise poison the same agent id here. Autouse for the same reason
    _clear_run_registry is: a future sync test that does not take `home` is exactly the one
    that would inherit the state and fail somewhere else."""
    from bridge import sync
    sync._SYNCED.clear()
    sync._MTIMES.clear()
    sync._CLI_PULLED.clear()
    yield
    sync._SYNCED.clear()
    sync._MTIMES.clear()
    sync._CLI_PULLED.clear()


@pytest.fixture
def posted(monkeypatch):
    """Capture every hook-token callback instead of doing HTTP."""
    from bridge import turn
    calls = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: (calls.append((path, body)), 200)[1])
    return calls


@pytest.fixture(autouse=True)
def _clear_run_registry():
    """turn.RUNS is process-global mutable state, and tests/factories.py's f.ingress() always
    uses the same fixed runId -- without this, a test that leaves an entry "aborted" (e.g. an
    abort test) can silently poison a LATER, unrelated test that reuses the same run id via
    the default factory (surfaced by the review-fix round: test_ambient_gate.py's tests
    started reporting outcome "aborted" instead of their own outcome once test_abort.py grew
    more abort-leaving-state tests, purely from alphabetical test-file execution order)."""
    from bridge import turn
    turn.RUNS.clear()
    turn._PENDING_CALL_RECORDS.clear()
    turn._lock_since.clear()
    with turn._busy_lock:
        turn._busy = 0
    yield
    turn.RUNS.clear()
    turn._PENDING_CALL_RECORDS.clear()
    turn._lock_since.clear()
    with turn._busy_lock:
        turn._busy = 0


@pytest.fixture(autouse=True)
def _room_without_session_db(request, monkeypatch):
    """Fake agents carry no session DB; a room turn would otherwise try to record into one.
    test_native_sessions' ``hermes`` fixture runs the real recording path."""
    if "hermes" not in request.fixturenames:
        from bridge import turn
        monkeypatch.setattr(turn, "_observe_room", lambda *a: None)
        monkeypatch.setattr(turn, "_room_history", lambda _agent: ([], None))
        monkeypatch.setattr(turn, "_carry_call_records", lambda *a: None)
        monkeypatch.setattr(turn, "_record_call_exchange", lambda *a: None)


@pytest.fixture(autouse=True)
def run_events(monkeypatch):
    """v1 runs post heartbeat/terminal events to the backend (bridge/runs.py): recorded here, never sent.
    tests/test_run_events.py exercises the real poster against a fake urlopen."""
    from bridge import runs

    sent: list[tuple[dict, dict]] = []
    monkeypatch.setattr(runs, "_post_event", lambda identity, body: sent.append((identity, body)) or 200)
    return sent
