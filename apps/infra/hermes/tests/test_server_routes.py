from bridge import server

AUTH = {"authorization": "Bearer gw-test"}


def test_rpc_requires_the_gateway_bearer():
    assert server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", {}, {"method": "health"})[0] == 401


def test_health_answers_ready(home):
    # Needs the `home` fixture now that health answers {ok:false, NOT_READY} instead of
    # {ok:true, payload:{ready:false}} when boot isn't ready (see test_rpc_transport.py for
    # the not-ready case) -- container_rpc.call only raises on {ok:false}, so a bridge with
    # no profiles dir must not claim ok:true here.
    code, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH, {"method": "health"})
    assert code == 200 and obj["ok"] is True and obj["payload"]["runtime"] == "hermes"
    assert obj["payload"]["diskFreeMb"] > 0 and obj["payload"]["diskFreeInnerMb"] > 0


def test_unknown_path_is_404():
    assert server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", AUTH, {})[0] == 404


def test_the_default_s3_prefix_stays_inside_the_owner_grant():
    # The execution role is scoped to "<owner>/*" on the replicas bucket, so the old
    # "hermes/<owner>" default was outside it; only the runtime env override hid that.
    import bridge
    assert bridge.default_s3_prefix("org_test") == "org_test/hermes"
