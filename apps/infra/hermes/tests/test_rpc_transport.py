from bridge import server

AUTH = {"authorization": "Bearer gw-test"}
RPC = "/plugins/lightbulb-channel/rpc"


def call(method, params=None, **over):
    return server.handle_plugin(RPC, "POST", AUTH, {"method": method, "params": params or {}, **over})


def test_health_is_cheap_true_and_names_the_runtime(home):
    code, obj = call("health")
    assert code == 200 and obj["ok"] is True
    assert obj["payload"]["ready"] is True and obj["payload"]["runtime"] == "hermes"
    assert isinstance(obj["payload"]["uptimeS"], float) and obj["payload"]["busy"] == 0


def test_health_answers_not_ready_as_ok_false_while_boot_secrets_are_missing(home, monkeypatch):
    # container_rpc.call only raises on {ok:false} (or a transport failure) -- it never reads
    # the payload -- and readiness_state's _gateway_reachable treats "no exception" as ready.
    # A not-ready bridge answering {ok:true, payload:{ready:false}} would read as ready
    # fleet-wide, so not-ready MUST be an {ok:false} error, not a false field in an ok payload.
    monkeypatch.setattr(server, "_boot_ready", lambda: False)
    code, obj = call("health")
    assert code == 200
    assert obj == {"ok": False, "error": {"code": "NOT_READY", "message": "ingress cannot be accepted yet"}}


def test_an_unregistered_method_answers_the_stable_unavailable_shape():
    """Every control-panel surface this runtime does not serve arrives here: they are not
    registered, and `_rpc`'s `handler is None` branch is the one place that answers them."""
    for method in ("tools.catalog", "sessions.list", "skills.install", "browser.request",
                   "doctor.memory.status", "chat.history", "config.get", "config.patch",
                   "agents.list", "agents.delete", "provider.freeze", "channels.status",
                   "skills.update", "skills.proposals.list"):
        code, obj = call(method)
        assert code == 200
        assert obj == {"ok": False, "error": {"code": "UNAVAILABLE",
                                              "message": f"{method} is not available on the Hermes runtime"}}
        assert method not in server.RPC_METHODS


def test_an_empty_method_or_non_object_params_is_invalid_request():
    assert call("")[1]["error"]["code"] == "INVALID_REQUEST"
    assert server.handle_plugin(RPC, "POST", AUTH, {"method": "health", "params": []})[0] == 400


def test_a_handler_exception_never_leaks_its_text():
    server.RPC_METHODS["boom"] = lambda params, **_kwargs: (_ for _ in ()).throw(ValueError("secret=hunter2"))
    try:
        code, obj = call("boom")
        assert code == 200 and obj["error"]["code"] == "UNAVAILABLE"
        assert "hunter2" not in obj["error"]["message"]
    finally:
        server.RPC_METHODS.pop("boom")


def test_an_agent_timeout_maps_to_http_504():
    server.RPC_METHODS["slow"] = lambda params, **_kwargs: {"ok": False, "error": {"code": "AGENT_TIMEOUT", "message": "x"}}
    try:
        assert call("slow")[0] == 504
    finally:
        server.RPC_METHODS.pop("slow")


def test_an_unknown_method_is_unavailable_not_404():
    code, obj = call("totally.unknown")
    assert code == 200 and obj["error"]["code"] == "UNAVAILABLE"


def _echo_timeout(seen):
    return lambda params, timeout_ms=None, **_kwargs: (seen.append(timeout_ms), {"ok": True, "payload": {}})[1]


def test_timeout_ms_defaults_when_absent_and_is_passed_to_the_handler():
    seen = []
    server.RPC_METHODS["echo_timeout"] = _echo_timeout(seen)
    try:
        call("echo_timeout")
        assert seen == [server.RPC_DEFAULT_TIMEOUT_MS]
    finally:
        server.RPC_METHODS.pop("echo_timeout")


def test_timeout_ms_above_the_max_is_clamped():
    seen = []
    server.RPC_METHODS["echo_timeout"] = _echo_timeout(seen)
    try:
        call("echo_timeout", timeoutMs=999_999)
        assert seen == [server.RPC_MAX_TIMEOUT_MS]
    finally:
        server.RPC_METHODS.pop("echo_timeout")


def test_timeout_ms_within_range_passes_through_unclamped():
    seen = []
    server.RPC_METHODS["echo_timeout"] = _echo_timeout(seen)
    try:
        call("echo_timeout", timeoutMs=5_000)
        assert seen == [5_000]
    finally:
        server.RPC_METHODS.pop("echo_timeout")


def test_a_non_positive_or_non_integer_timeout_ms_is_invalid_request():
    assert call("health", timeoutMs=0)[0] == 400
    assert call("health", timeoutMs=-5)[0] == 400
    assert call("health", timeoutMs="soon")[0] == 400
    assert call("health", timeoutMs=True)[0] == 400
