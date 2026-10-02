"""Server-side half of Tasks 3/9/10/11: wiring lane A's turn.py pipeline (parse_ingress, the
RUNS registry, abort_run/steer_run) into lane B's handle_plugin routing. Mirrors
test_server_routes.py's style and conftest's `home`/`posted`/autouse-RUNS-clearing fixtures."""
import json

from bridge import server, turn
from tests import factories as f

AUTH = {"authorization": "Bearer gw-test"}
RUN = "22222222-2222-4222-8222-222222222222"


def _inbound_path(agent_id: str) -> str:
    return f"/plugins/lightbulb-channel/inbound/{agent_id}"


def _capturing_threading():
    """A `threading`-shaped stand-in whose Thread never actually runs its target -- start()
    just records the call. This patches the NAME inside the `server` module's namespace, not
    the global `threading` module object, so turn.py's own `import threading` (used by
    run_turn's real heartbeat thread) is completely untouched.

    Lets a test assert exactly what handle_plugin did synchronously, before any turn code
    ran, then run the captured target on demand with `.run_now()` -- no real threads, no
    joins, no timing races."""
    instances: list = []

    class _Thread:
        def __init__(self, *a, **kw) -> None:
            self._target = kw.get("target")
            self._args = kw.get("args", ())
            self.name = kw.get("name")
            instances.append(self)

        def start(self) -> None:
            pass

        def run_now(self) -> None:
            self._target(*self._args)

    class _Module:
        Thread = _Thread

    return _Module, instances


# ---- Step 1: ingress validation (Task 3's server half) ----------------------------------------
def test_a_body_missing_event_id_is_400_invalid_ingress(home):
    body = f.ingress()
    del body["eventId"]
    code, obj = server.handle_plugin(_inbound_path("ceo"), "POST", AUTH, body)
    assert code == 400
    assert obj["error"] == "invalid_ingress"


def test_a_target_agent_id_not_matching_the_path_is_400(home):
    body = f.ingress()
    code, obj = server.handle_plugin(_inbound_path("not-ceo"), "POST", AUTH, body)
    assert code == 400
    assert obj["error"] == "invalid_ingress"


def test_a_valid_ingress_body_is_accepted(home, posted, monkeypatch):
    mod, _created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    body = f.ingress()
    code, obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    assert obj["status"] == "accepted"
    assert obj["runId"] == body["run"]["runId"]
    # Finding 3 (minor): without the `posted` fixture this reaches the real (unmocked)
    # turn._post_backend for the accept-time run.started post -- real urllib against the
    # configured (fake) API host, up to 3 retries. Assert against the captured call instead.
    assert posted[0][1]["type"] == "run.started"


# ---- Step 2: replay check on the registry + run.started at accept (Task 9's server half) ------
def test_run_started_is_posted_synchronously_at_accept_before_the_turn_thread_ever_runs(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    body = f.ingress()
    code, _obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    assert len(created) == 1  # the turn thread was constructed but never run (start() is a no-op)
    assert len(posted) == 1
    assert posted[0][0] == "runs/org_test/events"
    assert posted[0][1]["type"] == "run.started"
    assert posted[0][1]["runId"] == body["run"]["runId"]


def test_run_started_is_not_duplicated_once_the_turn_actually_runs(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    body = f.ingress()
    code, _obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    created[0].run_now()  # execute the (real) turn.run_turn synchronously now
    started = [b for _p, b in posted if b.get("type") == "run.started"]
    assert len(started) == 1


def test_a_replayed_run_id_answers_200_and_constructs_no_second_thread(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    body = f.ingress()
    path = _inbound_path(body["targetAgentId"])
    code1, _obj1 = server.handle_plugin(path, "POST", AUTH, body)
    assert code1 == 202
    code2, obj2 = server.handle_plugin(path, "POST", AUTH, body)
    assert code2 == 200
    assert obj2 == {"eventId": body["eventId"], "runId": body["run"]["runId"], "status": "replayed"}
    assert len(created) == 1  # the replay never even constructs a second Thread


# ---- Step 3: local-session token header (Task 11's server half) -------------------------------
def test_the_local_session_token_header_is_read_case_insensitively_and_passed_to_run_turn(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    seen = {}

    def fake_run_hermes(agent_id, body, frames):
        seen["ctx"] = body["_turnContext"]  # written by _turn_context_file under the turn lock
        return "completed", None, "ack", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    body = f.ingress()
    # deliberately lowercase, unlike the canonical `X-Lightbulb-Local-Session-Token` spelling
    headers = {**AUTH, "x-lightbulb-local-session-token": "jwt-xyz-999"}
    code, _obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", headers, body)
    assert code == 202
    created[0].run_now()
    assert seen["ctx"]["localSessionToken"] == "jwt-xyz-999"


# ---- Step 4: abort and steer routes (Tasks 9/10's server halves) ------------------------------
def _abort_path(run_id: str) -> str:
    return f"/plugins/lightbulb-channel/runs/{run_id}/abort"


def _steer_path(run_id: str) -> str:
    return f"/plugins/lightbulb-channel/runs/{run_id}/steer"


def test_abort_of_a_registered_unattached_run_is_202_and_marks_it_aborted():
    turn.register_run(RUN)
    code, obj = server.handle_plugin(_abort_path(RUN), "POST", AUTH, {})
    assert code == 202
    assert obj == {"runId": RUN, "state": "aborting"}
    assert turn.RUNS[RUN]["aborted"] is True


def test_abort_of_an_unknown_run_is_404():
    code, obj = server.handle_plugin(_abort_path(RUN), "POST", AUTH, {})
    assert code == 404
    assert obj == {"runId": RUN, "state": "not_found"}


def test_steer_with_empty_text_is_400_invalid_steer():
    turn.register_run(RUN)
    code, obj = server.handle_plugin(_steer_path(RUN), "POST", AUTH, {"text": "   "})
    assert code == 400
    assert obj["error"] == "invalid_steer"


def test_steer_over_max_text_bytes_is_400_invalid_steer():
    turn.register_run(RUN)
    body = {"text": "x" * (turn.MAX_TEXT_BYTES + 1)}
    code, obj = server.handle_plugin(_steer_path(RUN), "POST", AUTH, body)
    assert code == 400
    assert obj["error"] == "invalid_steer"


def test_steer_of_a_running_run_delivers_the_text_to_the_attached_agent():
    turn.register_run(RUN)

    class FakeAgent:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def steer(self, text: str) -> bool:
            self.seen.append(text)
            return True

    agent = FakeAgent()
    turn.attach_agent(RUN, agent)
    code, obj = server.handle_plugin(_steer_path(RUN), "POST", AUTH, {"text": "  use staging  "})
    assert code == 202
    assert obj == {"runId": RUN, "state": "accepted"}
    assert agent.seen == ["use staging"]


def test_steer_of_a_queued_voice_run_is_kept_for_its_user_message():
    identity = {"runId": RUN, "agentId": "ceo", "roomId": f.ROOM, "family": "voice",
                "sessionId": turn.lightbulb_session_key("ceo", f.ROOM)}
    turn.register_call_run(identity)
    code, _ = server.handle_plugin(_steer_path(RUN), "POST", AUTH, {"text": "Aden (voice): also X"})
    assert code == 202
    assert turn.attach_agent(RUN, object()) == "Aden (voice): also X"


def test_steer_of_an_unknown_run_is_404():
    code, obj = server.handle_plugin(_steer_path(RUN), "POST", AUTH, {"text": "hi"})
    assert code == 404
    assert obj == {"runId": RUN, "state": "not_found"}


# ---- Fix round 1 --------------------------------------------------------------------------------
# Finding 1 (Critical): agent_mail carries no `run` block at all (turn.parse_ingress accepts it,
# see the MAIL fixture in test_agent_mail.py and turn.py's EVENT_KINDS comment), and lane D's
# agent_mail_delivery.py posts exactly that body to this door. `body["run"]["runId"]` raised
# KeyError('run') on it.
MAIL = {"version": 1, "eventId": "77777777-7777-4777-8777-777777777777",
        "eventKind": "agent_mail", "targetAgentId": "ceo",
        "sessionKey": "lightbulb-email:thread-42",
        "message": {"text": "Can you look at the invoice?", "fromAddress": "a@b.com",
                    "subject": "Invoice"}}


def test_an_agent_mail_ingress_with_no_run_block_is_accepted_with_no_run_started(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    code, obj = server.handle_plugin(_inbound_path("ceo"), "POST", AUTH, dict(MAIL))
    assert code == 202
    assert obj["status"] == "accepted"
    assert posted == []  # no run block -- nothing to register, nothing to post run.started for
    assert len(created) == 1


# ---- Fix round (Important 4): run-less ingress is deduped on eventId --------------------------
def test_a_redelivered_agent_mail_ingress_answers_replayed_and_spawns_no_second_thread(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    code1, obj1 = server.handle_plugin(_inbound_path("ceo"), "POST", AUTH, dict(MAIL))
    assert code1 == 202
    code2, obj2 = server.handle_plugin(_inbound_path("ceo"), "POST", AUTH, dict(MAIL))
    assert code2 == 200
    assert obj2 == {"eventId": MAIL["eventId"], "runId": None, "status": "replayed"}
    assert len(created) == 1  # the replay never even constructs a second Thread
    assert posted == []  # still no run.started -- a run-less ingress never had one


def test_a_room_ingress_with_a_run_block_still_posts_run_started_first(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    body = f.ingress()
    code, _obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    assert len(created) == 1
    assert len(posted) == 1
    assert posted[0][1]["type"] == "run.started"
    assert posted[0][1]["runId"] == body["run"]["runId"]


# Finding 2 (Important): server.py mutated turn.RUNS[run_id]["started"] = True directly,
# bypassing _RUNS_LOCK -- the lock every other registry mutator (register_run, attach_agent,
# finish_run, abort_run) takes.
def test_mark_started_sets_the_flag_on_a_registered_run():
    turn.register_run(RUN)
    turn.mark_started(RUN)
    assert turn.RUNS[RUN]["started"] is True


def test_mark_started_on_an_unknown_run_does_not_raise():
    turn.mark_started(RUN)  # no entry at all -- must be a no-op, not a KeyError


# ---- Final-review fix round: C3 -----------------------------------------------------------------
# routers/messaging.py:631-638 (post_paperclip_run) builds exactly this body -- copied literally
# here, not paraphrased -- and posts it to this same inbound door. Before the fix,
# "paperclip_run" was not in turn.EVENT_KINDS at all, so parse_ingress raised and this door
# answered 400 invalid_ingress for every Paperclip-triggered run.
PAPERCLIP_RUN_BODY = {
    "version": 1,
    "eventId": "run_123",
    "eventKind": "paperclip_run",
    "targetAgentId": "agent_ceo",
    "sessionKey": "agent:agent_ceo:paperclip:run_123",
    "message": {"text": "New comment on your task"},
}


def test_a_paperclip_run_ingress_with_no_room_or_run_block_is_accepted(home, posted, monkeypatch):
    mod, created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    code, obj = server.handle_plugin(_inbound_path("agent_ceo"), "POST", AUTH, dict(PAPERCLIP_RUN_BODY))
    assert code == 202
    assert obj["status"] == "accepted"
    assert posted == []  # no run block -- nothing to register, nothing to post run.started for
    assert len(created) == 1
