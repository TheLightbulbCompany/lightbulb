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


def test_health_is_false_while_boot_secrets_are_missing(home, monkeypatch):
    monkeypatch.setattr(server, "_boot_ready", lambda: False)
    assert call("health")[1]["payload"]["ready"] is False


def test_every_ui_only_method_answers_the_same_stable_shape():
    for method in sorted(server.UI_ONLY_METHODS):
        code, obj = call(method)
        assert code == 200
        assert obj == {"ok": False, "error": {"code": "UNAVAILABLE",
                                              "message": f"{method} is not available on the Hermes runtime"}}


def test_an_empty_method_or_non_object_params_is_invalid_request():
    assert call("")[1]["error"]["code"] == "INVALID_REQUEST"
    assert server.handle_plugin(RPC, "POST", AUTH, {"method": "health", "params": []})[0] == 400


def test_a_handler_exception_never_leaks_its_text():
    server.RPC_METHODS["boom"] = lambda params: (_ for _ in ()).throw(ValueError("secret=hunter2"))
    try:
        code, obj = call("boom")
        assert code == 200 and obj["error"]["code"] == "UNAVAILABLE"
        assert "hunter2" not in obj["error"]["message"]
    finally:
        server.RPC_METHODS.pop("boom")


def test_an_agent_timeout_maps_to_http_504():
    server.RPC_METHODS["slow"] = lambda params: {"ok": False, "error": {"code": "AGENT_TIMEOUT", "message": "x"}}
    try:
        assert call("slow")[0] == 504
    finally:
        server.RPC_METHODS.pop("slow")


def test_an_unknown_method_is_unavailable_not_404():
    code, obj = call("totally.unknown")
    assert code == 200 and obj["error"]["code"] == "UNAVAILABLE"
