"""M6 task 10-C: ``models.list`` on the bridge.

The catalog is Hermes's own for whatever provider the profile pins.
"""

from bridge import server, sync

AUTH = {"authorization": "Bearer gw-test"}
RPC = "/plugins/lightbulb-channel/rpc"


def call(params):
    return server.handle_plugin(RPC, "POST", AUTH, {"method": "models.list", "params": params})


def test_a_byo_profile_lists_hermes_own_catalog_for_its_provider(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "claude-sonnet-5", "provider": "anthropic"}})
    monkeypatch.setattr(server, "_provider_catalog", lambda provider: [f"{provider}-a", f"{provider}-b"])

    payload = call({"agentId": "ceo"})[1]["payload"]

    assert payload == {
        "models": [
            {"id": "anthropic-a", "name": "anthropic-a", "provider": "anthropic"},
            {"id": "anthropic-b", "name": "anthropic-b", "provider": "anthropic"},
        ],
        "defaultId": "claude-sonnet-5",
    }


def test_no_agent_id_answers_from_any_profile_since_they_share_a_provider(home, monkeypatch):
    for name in ("ceo", "finance"):
        (home / "profiles" / name).mkdir(parents=True)
        sync.write_profile_config(name, {"model": {"default": f"m-{name}", "provider": "openai-api"}})
    monkeypatch.setattr(server, "_provider_catalog", lambda _provider: ["gpt-x"])

    payload = call({})[1]["payload"]

    assert [row["id"] for row in payload["models"]] == ["gpt-x"]


def test_an_unknown_profile_falls_back_rather_than_failing(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "m", "provider": "anthropic"}})
    monkeypatch.setattr(server, "_provider_catalog", lambda _provider: ["x"])

    assert call({"agentId": "ghost"})[1]["ok"] is True


def test_no_profiles_at_all_is_an_empty_list(home):
    assert call({})[1]["payload"] == {"models": [], "defaultId": ""}


def test_an_unavailable_catalog_degrades_to_the_current_model_only(home, monkeypatch):
    """`hermes_cli` is importable only inside the Hermes image, and the live fetchers behind it
    can fail. A picker showing just the current model beats an RPC that raises."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "claude-sonnet-5", "provider": "anthropic"}})

    # No monkeypatch: the real _provider_catalog runs, and hermes_cli is absent in this suite.
    payload = call({"agentId": "ceo"})[1]["payload"]

    assert payload == {"models": [], "defaultId": "claude-sonnet-5"}


def test_models_auth_status_reports_no_provider_without_a_profile(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "tok")
    _, obj = server.handle_plugin(RPC, "POST", AUTH, {"method": "models.authStatus", "params": {}})
    assert obj["payload"]["providers"] == []
