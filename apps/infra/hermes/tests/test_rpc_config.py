from bridge import server, sync

AUTH = {"authorization": "Bearer gw-test"}
RPC = "/plugins/lightbulb-channel/rpc"


def call(method, params):
    return server.handle_plugin(RPC, "POST", AUTH, {"method": method, "params": params})


def test_deep_merge_recurses_into_dicts_and_replaces_lists_wholesale():
    merged = sync.deep_merge({"a": {"b": 1, "c": 2}, "deny": ["x", "y"]},
                             {"a": {"c": 3, "d": 4}, "deny": ["z"]})
    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "deny": ["z"]}


def test_patch_then_get_round_trips_through_the_profile_yaml(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "aip-1"}, "terminal": {"backend": "local"}})
    code, obj = call("config.patch", {"agentId": "ceo", "patch": {"terminal": {"backend": "lightbulb_node"}}})
    assert code == 200 and obj["ok"] is True
    assert obj["payload"]["config"]["terminal"]["backend"] == "lightbulb_node"
    assert obj["payload"]["config"]["model"] == {"default": "aip-1"}
    assert call("config.get", {"agentId": "ceo"})[1]["payload"]["config"]["terminal"]["backend"] == "lightbulb_node"


def test_set_replaces_rather_than_merging(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "aip-1"}, "gone": True})
    call("config.set", {"agentId": "ceo", "config": {"model": {"default": "aip-2"}}})
    assert sync.read_profile_config("ceo") == {"model": {"default": "aip-2"}}


def test_get_without_an_agent_id_returns_every_profile(home):
    for name in ("ceo", "finance"):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"model": {"default": f"aip-{name}"}})
    config = call("config.get", {})[1]["payload"]["config"]
    assert set(config) == {"ceo", "finance"}


def test_an_unknown_profile_is_unavailable_not_a_crash(home):
    assert call("config.patch", {"agentId": "ghost", "patch": {}})[1]["error"]["code"] == "UNAVAILABLE"
