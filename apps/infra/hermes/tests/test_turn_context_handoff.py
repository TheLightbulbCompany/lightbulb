"""Task 11's turn.py half: the bridge writes/clears the per-turn context file the
`lightbulb` Hermes plugin (lane C, apps/infra/hermes/plugins/lightbulb) reads by session id.
The plugin itself is out of this lane's scope; this covers only the bridge side of the
file contract (path, shape, and that it never survives past the turn)."""
import json

from bridge import turn
from tests import factories as f

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


def test_run_turn_writes_the_context_file_for_the_duration_of_the_turn_only(home, posted, monkeypatch):
    seen = {}

    def fake_run_hermes(agent_id, body, frames):
        path = turn._turn_context_path(turn._profile_dir(agent_id), f"{body['room']['id']}:{agent_id}")
        seen["existed_during_turn"] = path.exists()
        seen["ctx"] = json.loads(path.read_text())
        return "completed", None, "ack", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    body = f.ingress()
    turn.run_turn("ceo", body)

    assert seen["existed_during_turn"] is True
    assert seen["ctx"]["roomId"] == body["room"]["id"]
    assert seen["ctx"]["agentId"] == "ceo"
    assert seen["ctx"]["runId"] == body["run"]["runId"]
    assert seen["ctx"]["threadRootMessageId"] == body["message"]["messageId"]

    final_path = turn._turn_context_path(turn._profile_dir("ceo"), f"{body['room']['id']}:ceo")
    assert not final_path.exists()  # cleared after the turn, whether it succeeded or not


def test_the_context_file_is_cleared_even_when_the_turn_raises(home, posted, monkeypatch):
    def boom(agent_id, body, frames):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(turn, "_run_hermes", boom)
    body = f.ingress()
    turn.run_turn("ceo", body)
    final_path = turn._turn_context_path(turn._profile_dir("ceo"), f"{body['room']['id']}:ceo")
    assert not final_path.exists()
