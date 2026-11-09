"""Task 11's turn.py half: the bridge writes/clears the per-turn context file the
`lightbulb` Hermes plugin (lane C, apps/infra/hermes/plugins/lightbulb) reads by session id.
The plugin itself is out of this lane's scope; this covers only the bridge side of the
file contract (path, shape, and that it never survives past the turn) -- except for the one
cross-package contract test below (Critical 1), which deliberately imports lane C's own
reader the way tests/test_plugin_lightbulb.py does, to prove the two sides agree on a name
neither literal alone can prove."""
import json
import pathlib
import sys

from bridge import turn
from tests import factories as f

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import turn_context  # noqa: E402

SESSION = "room-1:ceo"


def test_write_then_clear_round_trips_the_context_by_session_id(tmp_path):
    ctx = {"apiUrl": "https://api.invalid", "ownerId": "org_test", "hookToken": "h"}
    turn.write_turn_context(tmp_path, SESSION, ctx)
    path = turn._turn_context_path(tmp_path, SESSION)
    assert json.loads(path.read_text()) == ctx
    turn.clear_turn_context(tmp_path, SESSION)
    assert not path.exists()


def test_a_slash_bearing_session_id_never_creates_a_nested_path(tmp_path):
    turn.write_turn_context(tmp_path, "a/b", {})
    assert (tmp_path / turn.TURN_CONTEXT_DIRNAME / "a_b.json").exists()


# ---- fix round (Critical 1) -------------------------------------------------------------------
def test_the_plugins_own_reader_resolves_the_file_by_the_session_id__construct_agent_receives(
    home, posted, monkeypatch
):
    """The contract test the review asked for: drive run_turn with _construct_agent
    monkeypatched to CAPTURE its session_key (not recompute what the test expects it to be),
    then read the file back with lane C's own reader using that captured id. Before the fix,
    turn.py wrote under f"{room_id}:{agent_id}" while _construct_agent's session_id was the
    lightbulb session key -- two different strings -- so this failed with ctx is None."""
    captured = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            # Read while the turn is still in flight -- clear_turn_context runs in run_turn's
            # finally, after _run_hermes (and thus this call) returns.
            monkeypatch.setattr(turn_context, "_home", lambda: turn._profile_dir("ceo"))
            captured["ctx"] = turn_context.read(captured["session_key"])
            return {"api_calls": 1, "final_response": "ack"}

    def fake_construct_agent(agent_id, body, frames, session_key):
        captured["session_key"] = session_key
        return _FakeAgent()

    monkeypatch.setattr(turn, "_construct_agent", fake_construct_agent)
    body = f.ingress()
    turn.run_turn("ceo", body)

    ctx = captured["ctx"]
    assert ctx is not None  # None means the plugin's reader found nothing at that session id
    assert ctx["roomId"] == body["room"]["id"]
    assert ctx["agentId"] == "ceo"
    assert ctx["runId"] == body["run"]["runId"]
    assert ctx["threadRootMessageId"] == body["message"]["messageId"]
    assert "localSessionToken" in ctx

    final_path = turn._turn_context_path(turn._profile_dir("ceo"), captured["session_key"])
    assert not final_path.exists()  # cleared after the turn, whether it succeeded or not


def test_run_turn_writes_the_caller_supplied_local_session_token(home, posted, monkeypatch):
    """Review fix (cheap e): server.py will pass the X-Lightbulb-Local-Session-Token header
    value through at merge; the default None keeps every other test (which never passes it)
    working unchanged."""
    seen = {}

    def fake_run_hermes(agent_id, body, frames):
        seen["ctx"] = body["_turnContext"]  # written by _turn_context_file under the turn lock
        return "completed", None, "ack", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", f.ingress(), local_session_token="jwt-abc-123")
    assert seen["ctx"]["localSessionToken"] == "jwt-abc-123"


def test_the_context_file_is_cleared_even_when_the_turn_raises(home, posted, monkeypatch):
    def boom(agent_id, body, frames):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(turn, "_run_hermes", boom)
    body = f.ingress()
    session_key = turn._resolve_session_key("ceo", body)
    turn.run_turn("ceo", body)
    final_path = turn._turn_context_path(turn._profile_dir("ceo"), session_key)
    assert not final_path.exists()


def test_a_queued_turn_never_touches_the_running_turns_context_file(home, posted, monkeypatch):
    """Audit P1-1: run_turn used to write the file BEFORE queueing on the conversation's turn
    lock, so a second run for the same session overwrote the running turn's context, and the
    first turn's clear then left the queued one with NO_TURN. Now only the locked section
    (_turn_context_file) writes and clears it."""
    body = f.ingress()
    session_key = turn._resolve_session_key("ceo", body)
    path = turn._turn_context_path(turn._profile_dir("ceo"), session_key)
    written = []

    def fake_run_hermes(agent_id, b, frames):        # stands in for a turn still queued
        written.append(path.exists())
        return "completed", None, "ack", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", body)
    assert written == [False]
    with turn._turn_context_file("ceo", body["_turnContext"]):
        assert json.loads(path.read_text())["runId"] == body["run"]["runId"]
    assert not path.exists()


def test_concurrent_router_context_grants_are_session_isolated_and_never_synced(home):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from bridge import sync

    barrier = Barrier(2)
    def read_own(index):
        ctx = {"sessionKey": f"router-session-{index}", "runId": f"run-{index}",
               "meetingContextGrant": f"private-grant-{index}"}
        path = turn._turn_context_path(turn._profile_dir("ceo"), ctx["sessionKey"])
        with turn._turn_context_file("ceo", ctx):
            barrier.wait(timeout=5)
            assert json.loads(path.read_text()) == ctx
            assert sync.is_excluded(str(path.relative_to(turn._profile_dir("ceo"))))
            barrier.wait(timeout=5)
        assert not path.exists()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read_own, (1, 2)))
