from bridge import server, sync

AUTH = {"authorization": "Bearer gw-test"}
RPC = "/plugins/lightbulb-channel/rpc"


def call(method, params=None):
    return server.handle_plugin(RPC, "POST", AUTH, {"method": method, "params": params or {}})[1]


def test_agents_list_reads_the_profile_dirs_and_their_names(home):
    for name, display in (("ceo", "Ada"), ("finance", "Finn")):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"agent": {"name": display}, "model": {"default": f"aip-{name}"}})
    agents = {a["id"]: a for a in call("agents.list")["payload"]["agents"]}
    assert agents["ceo"]["name"] == "Ada" and agents["ceo"]["model"] == "aip-ceo"
    assert set(agents) == {"ceo", "finance"}


def test_agents_update_patches_the_agent_block_and_accepts_either_key_spelling(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"agent": {"name": "Ada"}, "model": {"default": "aip-1"}})
    call("agents.update", {"agent_id": "ceo", "name": "Ada Lovelace"})
    assert sync.read_profile_config("ceo")["agent"]["name"] == "Ada Lovelace"
    assert sync.read_profile_config("ceo")["model"] == {"default": "aip-1"}


def test_agents_delete_removes_the_profile_dir_and_its_s3_prefix(home, monkeypatch):
    (home / "profiles" / "gone").mkdir(parents=True)
    sync.write_profile_config("gone", {})
    removed = []
    monkeypatch.setattr(sync, "delete_profile_objects", lambda agent_id: removed.append(agent_id))
    assert call("agents.delete", {"agent_id": "gone"})["payload"] == {"deleted": True}
    assert not (home / "profiles" / "gone").exists() and removed == ["gone"]


def test_channels_status_is_honestly_empty_because_the_backend_owns_channel_ingress():
    assert call("channels.status", {"probe": True})["payload"] == {"channels": [], "probe": True}


def test_models_auth_status_reports_the_real_bedrock_gate_binding(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "tok")
    assert call("models.authStatus")["payload"]["providers"] == [
        {"provider": "bedrock", "status": "ok", "via": "bedrock-gate"}]
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    assert call("models.authStatus")["payload"]["providers"][0]["status"] == "missing"
