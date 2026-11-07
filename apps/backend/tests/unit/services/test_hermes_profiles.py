import re
import pytest

from core.services import hermes_profiles


def cfg(**kw):
    base = dict(agent_role="engineering", provider_choice="chatgpt_oauth")
    return hermes_profiles.build_profile_config(**(base | kw))


def test_model_is_written_as_a_dict_with_default_never_a_bare_string():
    assert cfg()["model"] == {"default": "gpt-5.6-sol", "provider": "openai-codex"}


@pytest.mark.parametrize("retired", ["bedrock_claude", "bedrock_trial", "anything_else"])
def test_a_retired_or_unknown_provider_choice_refuses_to_render(retired):
    """No silent fallthrough to the ChatGPT block for a choice nothing handles."""
    with pytest.raises(ValueError, match="unsupported provider_choice"):
        cfg(provider_choice=retired)


def test_managed_openrouter_refuses_to_render_without_server_model_and_gate(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "", raising=False)
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_GATE_URL", "", raising=False)
    with pytest.raises(ValueError, match="managed OpenRouter route"):
        cfg(provider_choice="openrouter_managed")


def test_managed_openrouter_primary_and_all_auxiliary_tasks_use_only_the_gate(monkeypatch):
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model", raising=False)
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1", raising=False)
    config = cfg(provider_choice="openrouter_managed")
    assert config["model"] == {
        "default": "candidate/model",
        "provider": "custom",
        "base_url": "https://gate.example/v1",
        "api_key": "${LIGHTBULB_GATE_BEARER}",
        "context_length": 200_000,
    }
    for task in set(_REAL_AUXILIARY_TASKS) - {"vision"}:
        assert config["auxiliary"][task] == {
            "provider": "custom",
            "model": "candidate/model",
            "base_url": "https://gate.example/v1",
            "api_key": "${LIGHTBULB_GATE_BEARER}",
            "fallback_chain": [],
        }
    assert "vision" not in config["auxiliary"]  # never pinned, for any provider -- owner ruling 2026-09-28
    # The gate admits only streaming requests; this forces Hermes's plain aux calls to stream.
    assert config["auxiliary"]["stream_only_base_urls"] == ["gate.example"]
    assert "bedrock" not in config
    assert "sk-or-" not in str(config)


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


def test_approvals_are_off_with_an_unattended_approve_and_a_deny_backstop():
    approvals = cfg()["approvals"]
    assert approvals["mode"] == "off"
    assert approvals["unattended_mode"] == "approve"
    assert approvals["deny"] == list(hermes_profiles.APPROVALS_DENY)


def test_the_tool_deny_list_is_ported_to_disabled_toolsets():
    assert set(cfg()["agent"]["disabled_toolsets"]) == {"cronjob", "tts", "video_gen", "clarify"}
    assert cfg()["cron"]["allow_agent_scheduling"] is False


def test_no_dashboard_key_is_emitted():
    """I5 (minor): the real ``dashboard`` block (config_defaults.py:931) has no ``enabled`` field
    and nothing reads one — emitting ``{"enabled": False}`` was a false sense of coverage, not a
    real pin. Nothing launches the dashboard in the microVM regardless, so the fix is to stop
    emitting a key that claims a control this module doesn't actually have."""
    assert "dashboard" not in cfg()


def test_limits_memory_skills_and_sessions_are_all_emitted():
    c = cfg()
    assert c["agent"]["run_budget_seconds"] == 1800
    assert c["max_concurrent_sessions"] == 3
    assert c["memory"]["memory_enabled"] is True and c["memory"]["provider"] == ""
    assert c["sessions"]["retention_days"] == 90 and c["sessions"]["vacuum_after_prune"] is True
    assert c["skills"]["write_approval"] is False


def test_skills_external_dirs_registers_ours_and_hermes_bundled():
    """Ours (baked, image-immutable) plus Hermes's own 58 bundled skills, since our bridge never
    runs the CLI's normal ``tools.skills_sync.sync_skills`` copy-in step -- registering the
    package's own skills dir is what makes them visible at all. List order is COSMETIC, not a
    precedence rule: ``skills_tool._locate_skill`` REFUSES a name that exists in more than one
    dir as ambiguous rather than picking the first match, so a same-named skill in both dirs must
    be resolved by loading its categorised path (e.g. ``productivity/pdf``), not by this order."""
    assert cfg()["skills"]["external_dirs"] == ["/opt/hermes-skills", "/opt/hermes/skills"]
    assert cfg()["skills"]["external_dirs"] == hermes_profiles.SKILLS_EXTERNAL_DIRS


def test_no_secret_ever_lands_in_config_yaml():
    import json

    c = cfg(
        mcp_servers={"stripe": {"url": "https://mcp.stripe.com", "headers": {"Authorization": "${STRIPE_MCP_TOKEN}"}}}
    )
    blob = json.dumps(c)
    assert "sk-" not in blob and "pcp_" not in blob and "Bearer " not in blob
    assert c["mcp_servers"]["stripe"]["headers"]["Authorization"] == "${STRIPE_MCP_TOKEN}"


def test_byo_key_selects_the_direct_provider_and_its_model_ids():
    c = cfg(provider_choice="byo_key", byo_provider="anthropic")
    assert c["model"] == {"default": "claude-sonnet-5", "provider": "anthropic"}
    assert "bedrock" not in c


def test_byo_key_premium_and_cheap_tiers_come_from_the_shared_catalog_not_a_local_copy(monkeypatch):
    """I4: premium_model_ref/cheap_model_ref are the single source of
    truth for these ids elsewhere in the codebase — their own docstrings record two catalog
    rotations that already broke a hardcoded ref ("fails every Friday run until discovery
    refreshes"). Monkeypatching the shared functions and seeing the change reflected here proves
    this module holds no second copy of its own."""
    monkeypatch.setattr(hermes_profiles, "premium_model_ref", lambda *a, **k: "anthropic/PATCHED-PREMIUM")
    monkeypatch.setattr(hermes_profiles, "cheap_model_ref", lambda *a, **k: "anthropic/PATCHED-CHEAP")
    c = cfg(provider_choice="byo_key", byo_provider="anthropic", agent_role="ceo", is_ceo=True)
    assert c["model"]["default"] == "PATCHED-PREMIUM"
    assert c["auxiliary"]["compression"]["model"] == "PATCHED-CHEAP"


def test_chatgpt_oauth_selects_the_codex_provider_with_no_tier_split():
    c = cfg(provider_choice="chatgpt_oauth", is_ceo=True)
    assert c["model"]["provider"] == "openai-codex"


def test_chatgpt_oauth_pins_every_aux_task_to_the_codex_provider_not_auto():
    """Task 15, new ruling from the re-review: provider: auto on the aux tasks re-opens the
    OpenRouter/Nous auto-chain (auxiliary_client.py exempts "auto" from the raise a pinned,
    unavailable provider would otherwise hit). openai-codex is a real, explicit aux provider —
    verified in the image: hermes_cli/auth.py's ProviderConfig("openai-codex", ...),
    auxiliary_client.py's dedicated resolution branch for it, and its own comment "openai-codex
    has no safe default model; callers set auxiliary.<task>.model" (confirming an explicit model
    is the supported, intended usage — not a guess). Pinned to the SAME model the primary uses
    (gpt-5.6-sol): chatgpt_oauth is flat-rate with no cheaper tier to route aux work to, the same
    reasoning the main model block already uses."""
    c = cfg(provider_choice="chatgpt_oauth", is_ceo=True)
    for task in set(hermes_profiles.AUXILIARY_TASKS) - {"vision"}:
        assert c["auxiliary"][task]["provider"] == "openai-codex", task
        assert c["auxiliary"][task]["model"] == "gpt-5.6-sol", task
        assert c["auxiliary"][task]["fallback_chain"] == [], task


def test_terminal_runs_locally_without_an_attached_node():
    assert cfg()["terminal"]["backend"] == "local"


def test_plugins_enabled_is_a_flat_list_never_entries():
    c = cfg()
    assert c["plugins"] == {"enabled": ["lightbulb"]}


def test_profile_env_has_no_paperclip_key():
    """Paperclip exit (task 8b.2): a profile carries no Paperclip credential, only the provisioned env."""
    env = hermes_profiles.build_profile_env(env_vars={"STRIPE_SECRET_KEY": "sk_live_x"})
    assert env == {"STRIPE_SECRET_KEY": "sk_live_x"}


def test_dotenv_carries_the_byo_key_under_the_right_provider_env_name():
    anthropic_env = hermes_profiles.build_profile_env(env_vars={}, byo_provider="anthropic", byo_api_key="sk-ant-x")
    assert anthropic_env["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert "OPENAI_API_KEY" not in anthropic_env

    openai_env = hermes_profiles.build_profile_env(env_vars={}, byo_provider="openai", byo_api_key="sk-oai-x")
    assert openai_env["OPENAI_API_KEY"] == "sk-oai-x"
    assert "ANTHROPIC_API_KEY" not in openai_env


def test_dotenv_omits_a_byo_key_slot_when_no_byo_key_is_given():
    env = hermes_profiles.build_profile_env(env_vars={})
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env


def test_dotenv_never_carries_the_process_wide_gate_endpoint_region_bearer_or_a_secret_name_ref():
    """C3: AWS_ENDPOINT_URL_BEDROCK_RUNTIME/AWS_REGION never reach ``os.environ`` from a profile's
    ``.env`` — ``agent/secret_scope.py`` builds a dict-only mapping that is never installed as
    process env by the bridge's direct ``AIAgent`` construction, so botocore/``bedrock_adapter.py``
    would read the REAL AWS endpoint instead of the gate. Both belong in Task 17's runtime
    ``environmentVariables``, set once per container, not per agent profile. The bearer arrives the
    same way, via the boot secret — never through this function at all.
    C2: no ``LIGHTBULB_*_SECRET`` reference name either — that resolver was cancelled (M4-R4/R5)
    and nothing in ``bridge/`` ever read those names; every profile secret is now a resolved value."""
    env = hermes_profiles.build_profile_env(env_vars={})
    assert "AWS_ENDPOINT_URL_BEDROCK_RUNTIME" not in env
    assert "AWS_REGION" not in env
    assert "AWS_BEDROCK_FORCE_HTTP1" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "LIGHTBULB_GATE_BEARER_SECRET" not in env
    assert "LIGHTBULB_PAPERCLIP_KEY_SECRET" not in env
    assert "LIGHTBULB_BYO_KEY_SECRET" not in env


def test_render_dotenv_quotes_and_escapes():
    out = hermes_profiles.render_dotenv({"A": "one two", "B": 'say "hi"', "C": "plain"})
    assert out.splitlines() == ['A="one two"', 'B="say \\"hi\\""', "C=plain"]


def test_render_dotenv_refuses_an_illegal_key():
    import pytest

    with pytest.raises(ValueError):
        hermes_profiles.render_dotenv({"not a key": "x"})


# Fix round 1, I3: a small, faithful copy of Hermes's OWN ``.env`` grammar — the exact functions
# every reader (profile scope, ``hermes_cli.config.load_env``, the dashboard scrub, ...) parses
# through — so the round-trip test below exercises the real algorithm, not our own assumptions
# about it. Verified against the image:
#   docker run --rm --entrypoint grep lightbulb/hermes-spike:m1-rc4 -n -A40 "def load_env_file" \
#     /opt/hermes/agent/secret_scope.py
def _hermes_strip_inline_comment(value: str) -> str:
    """Copy of ``agent/secret_scope.py:181-201``."""
    value = value.strip()
    if not value:
        return value
    quote = value[0]
    if quote in ("'", '"'):
        i = 1
        while i < len(value):
            ch = value[i]
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                return value[: i + 1] if value[i + 1 :].lstrip().startswith("#") else value
            i += 1
        return value
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _hermes_parse_env_value(raw_value: str) -> str:
    """Copy of ``agent/secret_scope.py:204-219``."""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        quoted = value[1:-1]
        parsed: list[str] = []
        i = 0
        while i < len(quoted):
            escaped = quoted[i] == "\\" and quoted[i + 1 : i + 2] in ('"', "\\")
            parsed.append(quoted[i + 1] if escaped else quoted[i])
            i += 2 if escaped else 1
        return "".join(parsed)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _hermes_load_env_file(text: str) -> dict:
    """Copy of ``agent/secret_scope.py:222-251`` ``load_env_file``, minus file I/O and BOM
    handling (``render_dotenv``'s output is already a decoded ``str``)."""
    secrets: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key:
            secrets[key] = _hermes_parse_env_value(_hermes_strip_inline_comment(value))
    return secrets


def test_render_dotenv_round_trips_through_the_real_hermes_parser_for_every_special_character():
    values = {
        "PLAIN": "abc123",
        "WITH_SPACE": "one two",
        "WITH_QUOTE": 'say "hi"',
        "WITH_BACKSLASH": r"C:\Users\x",
        "WITH_HASH": "value#not-a-comment",
        "WITH_EQUALS": "a=b=c",
        "LEADING_TRAILING_SPACE": "  padded  ",
        "TRAILING_BACKSLASH": "foo\\",
        "DOUBLE_TRAILING_BACKSLASH": "foo\\\\",
        "LITERAL_BACKSLASH_N_TEXT": "line1\\nline2",  # the two chars \ and n — NOT a real newline
        "HASH_WITH_LEADING_SPACE": "value # looks like a comment but is not",
    }
    rendered = hermes_profiles.render_dotenv(values)
    assert _hermes_load_env_file(rendered) == values


def test_render_dotenv_rejects_a_value_containing_a_real_newline():
    """``load_env_file`` splits on ``text.splitlines()`` BEFORE any value parsing
    (``secret_scope.py:241``), so an embedded newline byte has NO valid encoding in this grammar —
    it would truncate its own assignment and reinterpret every following line as a fresh
    ``KEY=VALUE`` (silently dropping the rest of the file, or injecting an attacker-chosen
    variable). Reject rather than silently corrupt the file."""
    import pytest

    with pytest.raises(ValueError):
        hermes_profiles.render_dotenv({"X": "line1\nline2"})


def test_render_dotenv_rejects_a_value_containing_a_carriage_return():
    import pytest

    with pytest.raises(ValueError):
        hermes_profiles.render_dotenv({"X": "line1\rline2"})


# Fix round 1, minors.


def test_build_profile_config_takes_no_agent_id_parameter():
    import inspect

    assert "agent_id" not in inspect.signature(hermes_profiles.build_profile_config).parameters


def test_build_profile_config_takes_no_listening_paid_parameter_and_emits_no_guard_agent_created():
    """``listening_paid`` mapped to ``skills.guard_agent_created`` (a real Hermes key,
    ``config_defaults.py:1373``, default False — gates ``skill_manager_tool``'s ``terminal()``
    execution path) — but listening is a per-ROOM chat mode enforced by the backend, unrelated to
    skill-creation gating. write_agent_profile never even passed the parameter, so the mapping was
    dead weight pretending to be a control it wasn't."""
    import inspect

    assert "listening_paid" not in inspect.signature(hermes_profiles.build_profile_config).parameters
    assert "guard_agent_created" not in cfg()["skills"]


def test_dotenv_carries_the_agent_id_and_no_paperclip_identity():
    """LIGHTBULB_AGENT_ID is the agent's id for the lightbulb-*.sh scripts; PAPERCLIP_AGENT_ID (same
    value) stays until every runtime runs an image that reads the new name (task 8b.9)."""
    files = hermes_profiles.write_agent_profile(
        "org_1",
        {"id": "agent-1", "name": "Friday", "role": "ceo", "companyId": "company-1"},
        provider_choice="chatgpt_oauth",
        integrations={},
        env_vars={},
    )
    assert "LIGHTBULB_AGENT_ID=agent-1" in files.dotenv and "PAPERCLIP_AGENT_ID=agent-1" in files.dotenv
    for gone in ("PAPERCLIP_API_KEY", "PAPERCLIP_API_URL", "PAPERCLIP_COMPANY_ID", "company-1"):
        assert gone not in files.dotenv


def test_stdio_mcp_servers_get_the_agents_profile_home():
    """One bridge process serves every agent with HOME=/, and Hermes spawns stdio MCP children
    from the process env, so a server reading $HOME (QuickBooks) must have it pinned per agent."""
    from core.services.hermes_profiles import with_profile_home

    servers = {
        "quickbooks": {"command": "/usr/local/bin/lightbulb-quickbooks-mcp", "env": {"X": "${MCP_Q_X}"}},
        "notion": {"url": "https://mcp.notion.com/mcp", "auth": "oauth"},
    }
    out = with_profile_home(servers, "agent-1")
    assert out["quickbooks"]["env"] == {"X": "${MCP_Q_X}", "HOME": "/opt/data/profiles/agent-1/home"}
    assert out["notion"] == servers["notion"]
    assert "HOME" not in servers["quickbooks"]["env"]  # input left untouched (shared across agents)


@pytest.mark.parametrize(
    ("provider_choice", "byo_provider"), [("chatgpt_oauth", None), ("byo_key", "anthropic"), ("byo_key", "openai")]
)
def test_no_provider_pins_auxiliary_vision(provider_choice, byo_provider):
    """Owner ruling 2026-09-28: "if we don't need the vision model then let's not pin it, we
    should just use whatever hermes natively uses here" -- ``auxiliary.vision`` is never written,
    for any provider. Hermes 0.21.3 routes EVERY computer_use screenshot through an aux
    description whenever auxiliary.vision is explicit (vision_routing.py step 1) -- prod
    2026-09-28: Friday on gpt-5.6-sol never saw a pixel. A missing key mirrors Hermes's own
    explicit-override test (vision_routing._explicit_aux_vision_override) exactly like the
    unset stub used to."""
    c = cfg(provider_choice=provider_choice, byo_provider=byo_provider)
    assert "vision" not in c["auxiliary"]
    assert c["auxiliary"]["compression"]["fallback_chain"] == []  # every other task stays pinned
    # openai-api is missing from Hermes's tool-result image table, so it needs the declared capability.
    assert c["model"].get("supports_vision") is (True if byo_provider == "openai" else None)


def test_managed_openrouter_also_never_pins_vision(monkeypatch):
    """The managed gate model is no longer special-cased (owner ruling 2026-09-28): Hermes's own
    auto vision routing decides for it too, same as every other provider."""
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model", raising=False)
    monkeypatch.setattr(hermes_profiles.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1", raising=False)
    c = cfg(provider_choice="openrouter_managed")
    assert "vision" not in c["auxiliary"]
    assert "supports_vision" not in c["model"]


def test_front_desk_section_names_the_number():
    out = hermes_profiles.front_desk_section("+15551234567")
    assert "text on +15551234567." in out
    assert "{number}" not in out
    assert "A text that reaches you directly is from a customer." in out
    assert "A workspace member's text arrives here as their room message, never through this channel." in out
    assert "Every texter is a customer" not in out


def test_build_soul_md_adds_the_front_desk_section_only_with_a_number():
    agent = {"id": "a1", "name": "Desk", "role": "general"}
    bare = hermes_profiles.build_soul_md(agent)
    desk = hermes_profiles.build_soul_md(agent, front_desk_number="+15551234567")
    assert desk == bare + "\n\n" + hermes_profiles.front_desk_section("+15551234567")
    assert hermes_profiles.is_generated_soul(desk) and "## Front desk" not in bare
