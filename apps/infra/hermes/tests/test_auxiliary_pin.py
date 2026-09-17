from bridge import sync

AUX_DEFAULTS = {
    "transient_retries": 2, "free_only": False, "openrouter_model": "", "stream_only_base_urls": [],
    "vision": {"provider": "auto", "model": "", "timeout": 120},
    "compression": {"provider": "auto", "model": "", "timeout": 120},
    "title_generation": {"enabled": True, "provider": "auto", "model": "", "timeout": 30},
    "review": {"provider": "auto", "model": "", "api_mode": ""},
}


def test_every_task_block_is_pinned_to_bedrock_with_an_empty_fallback_chain():
    pinned = sync.auxiliary_pin(AUX_DEFAULTS, "arn:aws:bedrock:...:application-inference-profile/haiku")
    for task in ("vision", "compression", "title_generation", "review"):
        assert pinned["auxiliary"][task]["provider"] == "bedrock"
        assert pinned["auxiliary"][task]["model"].endswith("/haiku")
        assert pinned["auxiliary"][task]["fallback_chain"] == []
    assert pinned["auxiliary"]["free_only"] is True


def test_scalar_auxiliary_keys_are_never_turned_into_task_blocks():
    pinned = sync.auxiliary_pin(AUX_DEFAULTS, "aip")["auxiliary"]
    assert "transient_retries" not in pinned and "stream_only_base_urls" not in pinned


def test_bedrock_discovery_is_off_because_the_owner_role_cannot_list_foundation_models():
    assert sync.auxiliary_pin(AUX_DEFAULTS, "aip")["bedrock"] == {"discovery": {"enabled": False}}


def test_pinning_merges_under_an_existing_profile_config_without_losing_it(home, monkeypatch):
    (home / "profiles" / "ceo").mkdir(parents=True)
    sync.write_profile_config("ceo", {"model": {"default": "aip-main"}, "auxiliary": {"vision": {"timeout": 300}}})
    monkeypatch.setattr(sync, "_aux_defaults", lambda: AUX_DEFAULTS)
    config = sync.pin_profile_defaults("ceo", "aip-aux")
    assert config["model"] == {"default": "aip-main"}
    assert config["auxiliary"]["vision"] == {"provider": "bedrock", "model": "aip-aux",
                                             "fallback_chain": [], "timeout": 300}
