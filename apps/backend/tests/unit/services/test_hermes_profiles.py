from core.services import hermes_profiles

AIPS = {
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/son",
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/opu",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/hai",
}


def cfg(**kw):
    base = dict(agent_id="a1", agent_role="engineering", provider_choice="bedrock_claude", aip_arns=AIPS)
    return hermes_profiles.build_profile_config(**(base | kw))


def test_model_is_written_as_a_dict_with_default_never_a_bare_string():
    assert cfg()["model"] == {"default": AIPS["sonnet"], "provider": "bedrock"}


def test_the_ceo_profile_is_pinned_to_the_premium_tier():
    assert cfg(agent_role="ceo", is_ceo=True)["model"]["default"] == AIPS["opus"]


def test_a_trial_owner_with_one_aip_keeps_every_tier_on_that_one_profile():
    trial = cfg(provider_choice="bedrock_trial", aip_arns={"sonnet": AIPS["sonnet"]}, is_ceo=True)
    assert trial["model"]["default"] == AIPS["sonnet"]
    assert trial["auxiliary"]["compression"]["model"] == AIPS["sonnet"]


def test_bedrock_discovery_is_pinned_off():
    assert cfg()["bedrock"]["discovery"]["enabled"] is False
    assert cfg()["bedrock"]["region"] == "us-east-1"


# Fix round 1, C1: the real 18 provider-bearing keys of Hermes 0.21.3's
# ``hermes_cli.config_defaults.DEFAULT_CONFIG["auxiliary"]``, verified directly against the image —
#   docker run --rm --entrypoint sed lightbulb/hermes-spike:m1-rc4 -n '679,750p' \
#     /opt/hermes/hermes_cli/config_defaults.py
# Hardcoded here (independent of ``hermes_profiles.AUXILIARY_TASKS``) so this test cannot pass
# tautologically — the previous version iterated the module constant itself, so it passed for
# any list, including the wrong 4-task one it was written to guard against.
_REAL_AUXILIARY_TASKS = (
    "approval",
    "background_review",
    "compression",
    "curator",
    "goal_judge",
    "kanban_decomposer",
    "mcp",
    "memory_query_rewrite",
    "moa_aggregator",
    "moa_reference",
    "monitor",
    "profile_describer",
    "review",
    "skills_hub",
    "title_generation",
    "triage_specifier",
    "tts_audio_tags",
    "vision",
)


def test_auxiliary_tasks_is_the_real_18_task_hermes_default_config_list():
    assert len(_REAL_AUXILIARY_TASKS) == 18
    assert hermes_profiles.AUXILIARY_TASKS == _REAL_AUXILIARY_TASKS


def test_every_one_of_the_18_auxiliary_tasks_is_pinned_to_bedrock_with_an_empty_fallback_chain():
    aux = cfg()["auxiliary"]
    assert aux["free_only"] is True
    for task in _REAL_AUXILIARY_TASKS:
        assert aux[task]["provider"] == "bedrock", task
        assert aux[task]["fallback_chain"] == [], task
        assert aux[task]["model"] == AIPS["haiku"], task


def test_approvals_are_off_with_an_unattended_approve_and_a_deny_backstop():
    approvals = cfg()["approvals"]
    assert approvals["mode"] == "off"
    assert approvals["unattended_mode"] == "approve"
    assert approvals["deny"] == list(hermes_profiles.APPROVALS_DENY)


def test_the_tool_deny_list_is_ported_to_disabled_toolsets():
    assert set(cfg()["agent"]["disabled_toolsets"]) == {"cronjob", "tts", "video_gen"}
    assert cfg()["dashboard"]["enabled"] is False
    assert cfg()["cron"]["allow_agent_scheduling"] is False


def test_limits_memory_skills_and_sessions_are_all_emitted():
    c = cfg()
    assert c["agent"]["run_budget_seconds"] == 1800
    assert c["max_concurrent_sessions"] == 3
    assert c["memory"]["memory_enabled"] is True and c["memory"]["provider"] == ""
    assert c["sessions"]["retention_days"] == 90 and c["sessions"]["vacuum_after_prune"] is True
    assert c["skills"]["write_approval"] is False


def test_no_secret_ever_lands_in_config_yaml():
    import json

    c = cfg(
        mcp_servers={"stripe": {"url": "https://mcp.stripe.com", "headers": {"Authorization": "${STRIPE_MCP_TOKEN}"}}}
    )
    blob = json.dumps(c)
    assert "sk-" not in blob and "pcp_" not in blob and "Bearer " not in blob
    assert c["mcp_servers"]["stripe"]["headers"]["Authorization"] == "${STRIPE_MCP_TOKEN}"


def test_byo_key_selects_the_direct_provider_and_its_model_ids():
    c = cfg(provider_choice="byo_key", byo_provider="anthropic", aip_arns=None)
    assert c["model"] == {"default": "claude-sonnet-5", "provider": "anthropic"}
    assert "bedrock" not in c


def test_chatgpt_oauth_selects_the_codex_provider_with_no_tier_split():
    c = cfg(provider_choice="chatgpt_oauth", aip_arns=None, is_ceo=True)
    assert c["model"]["provider"] == "openai-codex"
    assert c["auxiliary"]["compression"]["provider"] == "auto"


def test_plugins_enabled_is_a_flat_list_never_entries():
    c = cfg()
    assert c["plugins"] == {"enabled": ["lightbulb", "lightbulb_node"]}


def test_dotenv_carries_the_per_profile_secret_refs_and_the_provisioned_env():
    env = hermes_profiles.build_profile_env(
        gate_bearer_ref="lightbulb/dev/gate-bearer/org_a",
        paperclip_key_ref="lightbulb/dev/paperclip/org_a/a1",
        env_vars={"STRIPE_SECRET_KEY": "sk_live_x"},
    )
    assert env["LIGHTBULB_GATE_BEARER_SECRET"] == "lightbulb/dev/gate-bearer/org_a"
    assert env["LIGHTBULB_PAPERCLIP_KEY_SECRET"] == "lightbulb/dev/paperclip/org_a/a1"
    assert env["STRIPE_SECRET_KEY"] == "sk_live_x"


def test_dotenv_never_carries_the_process_wide_gate_endpoint_region_or_bearer():
    """C3: AWS_ENDPOINT_URL_BEDROCK_RUNTIME/AWS_REGION never reach ``os.environ`` from a profile's
    ``.env`` — ``agent/secret_scope.py`` builds a dict-only mapping that is never installed as
    process env by the bridge's direct ``AIAgent`` construction, so botocore/``bedrock_adapter.py``
    would read the REAL AWS endpoint instead of the gate. Both belong in Task 17's runtime
    ``environmentVariables``, set once per container, not per agent profile. The bearer arrives the
    same way, via the boot secret — never through this function at all."""
    env = hermes_profiles.build_profile_env(
        gate_bearer_ref="lightbulb/dev/gate-bearer/org_a",
        paperclip_key_ref="lightbulb/dev/paperclip/org_a/a1",
        env_vars={},
    )
    assert "AWS_ENDPOINT_URL_BEDROCK_RUNTIME" not in env
    assert "AWS_REGION" not in env
    assert "AWS_BEDROCK_FORCE_HTTP1" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env


def test_render_dotenv_quotes_and_escapes():
    out = hermes_profiles.render_dotenv({"A": "one two", "B": 'say "hi"', "C": "plain"})
    assert out.splitlines() == ['A="one two"', 'B="say \\"hi\\""', "C=plain"]


def test_render_dotenv_refuses_an_illegal_key():
    import pytest

    with pytest.raises(ValueError):
        hermes_profiles.render_dotenv({"not a key": "x"})
