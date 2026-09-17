import os, sys, pathlib, pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("GATEWAY_TOKEN", "gw-test")
os.environ.setdefault("ISOL8_OWNER_ID", "org_test")
os.environ.setdefault("ISOL8_API_URL", "https://api-dev.example.invalid")
os.environ.setdefault("OPENCLAW_HOOK_TOKEN", "hook-test")
os.environ.setdefault("S3_BUCKET", "")          # sync is a no-op without a bucket


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME, re-pointed on the already-imported modules."""
    from bridge import sync
    (tmp_path / "profiles").mkdir(parents=True)
    monkeypatch.setattr(sync, "HOME", tmp_path)
    return tmp_path


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
    yield
    turn.RUNS.clear()
