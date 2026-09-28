import pytest

from bridge import sync

AUX_DEFAULTS = {
    "transient_retries": 2, "free_only": False, "openrouter_model": "", "stream_only_base_urls": [],
    "vision": {"provider": "auto", "model": "", "timeout": 120},
    "compression": {"provider": "auto", "model": "", "timeout": 120},
    "title_generation": {"enabled": True, "provider": "auto", "model": "", "timeout": 30},
    "review": {"provider": "auto", "model": "", "api_mode": ""},
}


def test_plugins_pin_adds_lightbulb_plugins_to_empty_config():
    result = sync.plugins_pin({})
    assert result == {"plugins": {"enabled": ["lightbulb"]}}


def test_plugins_pin_preserves_existing_plugins_and_adds_lightbulb_ones():
    result = sync.plugins_pin({"plugins": {"enabled": ["foo", "lightbulb"], "disabled": ["bar"]}})
    assert result["plugins"]["enabled"] == ["foo", "lightbulb"]
    assert "disabled" not in result


def test_plugins_pin_deduplicates_and_preserves_order():
    result = sync.plugins_pin({"plugins": {"enabled": ["retired_plugin", "foo", "lightbulb"]}})
    assert result["plugins"]["enabled"] == ["retired_plugin", "foo", "lightbulb"]


def test_pinning_merges_under_an_existing_profile_config_without_losing_it(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "aip-main"}, "auxiliary": {"vision": {"timeout": 300}}})
    config = sync.pin_profile_defaults("ceo")
    assert config["model"] == {"default": "aip-main"}
    assert config["auxiliary"] == {"vision": {"timeout": 300}}


def test_pin_profile_defaults_adds_plugins_enabled_and_preserves_disabled(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"plugins": {"disabled": ["bar"]}, "auxiliary": {"vision": {"timeout": 300}}})
    config = sync.pin_profile_defaults("ceo")
    assert config["plugins"]["enabled"] == ["lightbulb"]
    assert config["plugins"]["disabled"] == ["bar"]
    assert config["auxiliary"] == {"vision": {"timeout": 300}}


# ---- fix round, M4-R2: pin_profile_defaults is FILL-MISSING, not force-pin --------------------
# M4's config writer (hermes_profiles.build_profile_config) is now the source of truth for a
# profile's aux pins, written at provision and kept current by Task 13's ensure_profile()
# re-pull on a changed .revision -- the bridge no longer needs to force-overwrite them every turn.
def test_pin_profile_defaults_never_overwrites_a_task_the_profile_already_pins(home):
    """The discriminating case: BOTH sides set every key on ``vision``, so the two merge
    directions disagree. Existing must win wholesale -- this is what actually distinguishes
    fill-missing from the old force-pin behaviour (every other case in this file has disjoint
    keys on the two sides and passes under either direction)."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config(
        "ceo", {"auxiliary": {"vision": {"provider": "openrouter", "model": "x", "fallback_chain": ["a"]}}}
    )
    config = sync.pin_profile_defaults("ceo")
    assert config["auxiliary"]["vision"] == {"provider": "openrouter", "model": "x", "fallback_chain": ["a"]}


def test_chatgpt_oauth_aux_is_left_to_hermes(home):
    """An unpinned task stays unpinned (Hermes's "auto" = the main provider)."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    codex = {"provider": "openai-codex", "model": "gpt-5.6-sol", "fallback_chain": []}
    sync.write_profile_config("ceo", {"model": {"provider": "openai-codex", "default": "gpt-5.6-sol"},
                                      "auxiliary": {"vision": codex, "free_only": True}})
    config = sync.pin_profile_defaults("ceo")
    assert config["auxiliary"] == {"vision": codex, "free_only": True}
    assert sync.read_profile_config("ceo")["auxiliary"] == config["auxiliary"]


def test_non_bedrock_profile_never_gains_a_bedrock_auxiliary_lane(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.setenv("LIGHTBULB_PROVIDER_CHOICE", "openrouter_managed")
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    monkeypatch.setenv("LIGHTBULB_MANAGED_MODEL_ID", "candidate")
    sync.write_profile_config("ceo", {"model": {"provider": "custom", "default": "candidate", "base_url": "https://gate.example/v1", "api_key": "${LIGHTBULB_GATE_BEARER}"},
                                      "auxiliary": {"vision": {"provider": "custom", "model": "candidate", "base_url": "https://gate.example/v1", "api_key": "${LIGHTBULB_GATE_BEARER}", "fallback_chain": []}}})
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)

    with pytest.raises(RuntimeError, match="missing auxiliary route"):
        sync.pin_profile_defaults("ceo")


def test_managed_custom_route_refuses_an_edited_upstream_url(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.setenv("LIGHTBULB_PROVIDER_CHOICE", "openrouter_managed")
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    monkeypatch.setenv("LIGHTBULB_MANAGED_MODEL_ID", "candidate")
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)
    tasks = {name: {"provider": "custom", "model": "candidate", "base_url": "https://gate.example/v1",
                    "api_key": "${LIGHTBULB_GATE_BEARER}", "fallback_chain": []}
             for name in ("vision", "compression", "title_generation", "review")}
    sync.write_profile_config("ceo", {"model": {"provider": "custom", "default": "candidate",
                                                "base_url": "https://attacker.example/v1",
                                                "api_key": "${LIGHTBULB_GATE_BEARER}"}, "auxiliary": tasks})

    with pytest.raises(RuntimeError, match="managed gate"):
        sync.pin_profile_defaults("ceo")


# "Managed" is read off the profile this turn just pulled, never the boot-time provider choice: a
# warm process outlives a provider switch, and the backend's profile rewrite lands on the next turn.
def test_warm_process_booted_on_bedrock_validates_a_pulled_managed_profile(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.setenv("LIGHTBULB_PROVIDER_CHOICE", "bedrock_trial")
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    monkeypatch.setenv("LIGHTBULB_MANAGED_MODEL_ID", "candidate")
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)
    tasks = {name: {"provider": "custom", "model": "candidate", "base_url": "https://gate.example/v1",
                    "api_key": "${LIGHTBULB_GATE_BEARER}", "fallback_chain": []}
             for name in ("vision", "compression", "title_generation", "review")}
    model = {"provider": "custom", "default": "candidate", "base_url": "https://gate.example/v1",
             "api_key": "${LIGHTBULB_GATE_BEARER}"}
    sync.write_profile_config("ceo", {"model": model, "auxiliary": tasks})

    config = sync.pin_profile_defaults("ceo")
    assert config["auxiliary"] == tasks  # validated as managed: no bedrock lane merged in

    tasks["review"] = {**tasks["review"], "base_url": "https://attacker.example/v1"}
    sync.write_profile_config("ceo", {"model": model, "auxiliary": tasks})
    with pytest.raises(RuntimeError, match="bypasses managed gate"):
        sync.pin_profile_defaults("ceo")


_GATE_ROUTE = {"provider": "custom", "model": "candidate", "base_url": "https://gate.example/v1",
               "api_key": "${LIGHTBULB_GATE_BEARER}", "fallback_chain": []}
_GATE_MODEL = {"provider": "custom", "default": "candidate", "base_url": "https://gate.example/v1",
               "api_key": "${LIGHTBULB_GATE_BEARER}"}


@pytest.mark.parametrize("vision", [
    None,  # hermes_profiles.auxiliary_tasks_block never writes vision (fresh profile)
    {"provider": "auto", "model": "", "base_url": "", "api_key": ""},  # AUX_VISION_UNSET
    # reapply_hermes_skills_dirs deep-merges AUX_VISION_UNSET over an old gate pin
    {"provider": "auto", "model": "", "base_url": "", "api_key": "", "fallback_chain": [], "timeout": 120},
])
def test_managed_profile_with_vision_unset_passes(home, monkeypatch, vision):
    """The exact prod shape (2026-09-28): every task gate-pinned except vision, which Hermes
    routes natively through the main provider -- the gate."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    monkeypatch.setenv("LIGHTBULB_MANAGED_MODEL_ID", "candidate")
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)
    tasks = {name: dict(_GATE_ROUTE) for name in ("compression", "title_generation", "review")}
    if vision is not None:
        tasks["vision"] = vision
    sync.write_profile_config("ceo", {"model": _GATE_MODEL, "auxiliary": {**tasks, "stream_only_base_urls": ["gate.example"]}})

    assert sync.pin_profile_defaults("ceo")["auxiliary"]["compression"] == _GATE_ROUTE


@pytest.mark.parametrize("vision", [
    {"provider": "openrouter", "model": "google/gemma-3-12b-it", "fallback_chain": []},
    {"provider": "auto", "model": "", "base_url": "https://attacker.example/v1"},
    {"provider": "auto", "model": "google/gemma-3-12b-it"},
    {"provider": "auto", "fallback_chain": [{"provider": "openrouter"}]},
    {**_GATE_ROUTE, "base_url": "https://attacker.example/v1"},
])
def test_managed_vision_route_off_the_gate_is_refused(home, monkeypatch, vision):
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.setenv("LIGHTBULB_MANAGED_GATE_URL", "https://gate.example/v1")
    monkeypatch.setenv("LIGHTBULB_MANAGED_MODEL_ID", "candidate")
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)
    tasks = {name: dict(_GATE_ROUTE) for name in ("compression", "title_generation", "review")}
    sync.write_profile_config("ceo", {"model": _GATE_MODEL, "auxiliary": {**tasks, "vision": vision}})

    with pytest.raises(RuntimeError, match="auxiliary route for vision"):
        sync.pin_profile_defaults("ceo")


def test_gate_bearer_route_without_a_gate_url_fails_closed(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    monkeypatch.delenv("LIGHTBULB_MANAGED_GATE_URL", raising=False)
    sync.write_profile_config("ceo", {"model": {"provider": "custom", "default": "candidate",
                                                "base_url": "https://gate.example/v1",
                                                "api_key": "${LIGHTBULB_GATE_BEARER}"}})
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)

    with pytest.raises(RuntimeError, match="managed gate"):
        sync.pin_profile_defaults("ceo")


# ---- #2749 fix-forward: a profile from before that PR can still carry the legacy backend ------
# #2749 made freshly written profiles default to terminal.backend: local and made the bridge
# honour whatever backend a profile's config.yaml names. Node/Mac execution is deferred and no
# product surface can set lightbulb_node any more, so an old profile that still carries it would
# route every shell command at a Mac node that isn't attached once the bridge starts honouring it.
def test_pin_profile_defaults_normalizes_legacy_lightbulb_node_terminal_backend(home):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"terminal": {"backend": "lightbulb_node"}})
    config = sync.pin_profile_defaults("ceo")
    assert config["terminal"]["backend"] == "local"


@pytest.mark.parametrize("terminal_block", [{"backend": "local"}, None])
def test_pin_profile_defaults_leaves_local_or_missing_terminal_backend_as_local(home, terminal_block):
    (home / "profiles" / "ceo").mkdir(parents=True)
    written = {"auxiliary": {"vision": {"timeout": 300}}}
    if terminal_block is not None:
        written["terminal"] = terminal_block
    sync.write_profile_config("ceo", written)
    config = sync.pin_profile_defaults("ceo")
    assert config.get("terminal", {}).get("backend", "local") == "local"


@pytest.mark.parametrize("browser", [None, {"backend": "browser-use", "cloud_provider": "agentcore", "engine": "auto"}])
def test_pin_profile_defaults_enforces_managed_browser_backend_and_preserves_siblings(home, browser):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"browser": browser, "model": {"default": "aip-main"}})

    config = sync.pin_profile_defaults("ceo")

    assert config["browser"]["backend"] == "off"
    assert config["model"] == {"default": "aip-main"}
    if browser:
        assert config["browser"]["cloud_provider"] == "agentcore"
        assert config["browser"]["engine"] == "auto"


def test_pinning_rewrites_config_only_when_it_changes(home, monkeypatch):
    """Audit P1-2: every turn rewrote config.yaml outside any lock, through one shared temp
    file -- two conversations starting together could crash one turn, or write a stale copy
    back over a hot patch the other had just pulled."""
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "aip-main"}})
    writes = []
    real = sync.write_profile_config
    monkeypatch.setattr(sync, "write_profile_config", lambda a, c: writes.append(c) or real(a, c))
    sync.pin_profile_defaults("ceo")          # first turn: normalizes and pins
    first = len(writes)
    sync.pin_profile_defaults("ceo")          # every later turn: nothing to change, no write
    assert first >= 1 and len(writes) == first
    assert not list((home / "profiles" / "ceo").glob("config.yaml*.tmp"))
