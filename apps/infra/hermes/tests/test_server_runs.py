"""Server-side half of Tasks 3/9/10/11: wiring lane A's turn.py pipeline (parse_ingress, the
RUNS registry, abort_run/steer_run) into lane B's handle_plugin routing. Mirrors
test_server_routes.py's style and conftest's `home`/`posted`/autouse-RUNS-clearing fixtures."""
from bridge import server, turn
from tests import factories as f

AUTH = {"authorization": "Bearer gw-test"}
RUN = "22222222-2222-4222-8222-222222222222"


def _inbound_path(agent_id: str) -> str:
    return f"/plugins/lightbulb-channel/inbound/{agent_id}"


class _NoopThread:
    """Replaces server.threading.Thread so a route test never actually launches a turn --
    only turn.py's OWN `import threading` (used by run_turn's real heartbeat) is left alone,
    because this patches the name inside the `server` module's namespace, not the global
    `threading` module object itself."""

    def __init__(self, *a, **kw) -> None:
        pass

    def start(self) -> None:
        pass


class _NoopThreading:
    Thread = _NoopThread


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
    turn._runs.clear()
    monkeypatch.setattr(server, "threading", _NoopThreading)
    body = f.ingress()
    code, obj = server.handle_plugin(_inbound_path(body["targetAgentId"]), "POST", AUTH, body)
    assert code == 202
    assert obj["status"] == "accepted"
    assert obj["runId"] == body["run"]["runId"]
