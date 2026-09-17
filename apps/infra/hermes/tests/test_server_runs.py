"""Server-side half of Tasks 3/9/10/11: wiring lane A's turn.py pipeline (parse_ingress, the
RUNS registry, abort_run/steer_run) into lane B's handle_plugin routing. Mirrors
test_server_routes.py's style and conftest's `home`/`posted`/autouse-RUNS-clearing fixtures."""
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


def test_a_valid_ingress_body_is_accepted(home, monkeypatch):
    mod, _created = _capturing_threading()
    monkeypatch.setattr(server, "threading", mod)
    body = f.ingress()
    code, obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    assert obj["status"] == "accepted"
    assert obj["runId"] == body["run"]["runId"]


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
