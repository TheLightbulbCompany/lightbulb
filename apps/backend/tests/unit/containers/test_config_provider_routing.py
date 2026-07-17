"""write_openclaw_config emits the correct provider block per provider_choice."""

import json

import pytest

from core.billing.bedrock_pricing import get_rate
from core.containers.config import (
    build_openclaw_config_dict,
    build_provider_block,
    cheap_model_ref,
    heartbeat_model_ref,
    premium_model_ref,
    write_openclaw_config,
)

# Per-owner AIP ARNs (Plan 2). The config builder now fails closed on the
# bedrock_claude branch unless these are threaded through, so every bedrock
# call below must pass aip_arns=_AIPS and the catalog ids become these ARNs.
_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}

# The catalog ids the bedrock branch emits once the owner's AIP ARNs are
# threaded in — the per-tier AIP ARN, not the system us.anthropic.* profile.
_VALID_BEDROCK_IDS = set(_AIPS.values())

# camelCase (config.py cost block) → snake_case (bedrock_pricing._RATES).
_COST_KEY_MAP = {
    "input": "input",
    "output": "output",
    "cacheRead": "cache_read",
    "cacheWrite": "cache_write",
}


@pytest.mark.asyncio
async def test_chatgpt_oauth_branch(tmp_path):
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="chatgpt_oauth",
        user_id="u_1",
    )
    cfg = json.loads(out.read_text())
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "openai/gpt-5.5"
    # OpenClaw's openai provider has no JSON config knob for the
    # auth dir — it reads the CODEX_HOME env var (set on the per-user ECS
    # task in ecs_manager). The provider block is omitted entirely so the
    # base schema validator (which requires baseUrl + models on a populated
    # entry) doesn't reject the empty `{}` we'd otherwise emit.
    assert "openai" not in cfg["models"].get("providers", {})


def test_config_keeps_cron_service_and_heartbeat():
    """The emitted config carries no top-level `cron` block (the in-container
    cron SERVICE stays on, governed by tools.deny) and keeps the 30m heartbeat
    cadence. The trial lockdown that switched both off for the deleted `meta`
    provider is re-homed onto the Bedrock trial config mode."""
    cfg = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
    )
    assert "cron" not in cfg
    assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "30m"


@pytest.mark.asyncio
async def test_byo_key_openai_branch(tmp_path):
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="byo_key",
        byo_provider="openai",
        user_id="u_1",
    )
    cfg = json.loads(out.read_text())
    model_block = cfg["agents"]["defaults"]["model"]
    # Primary stays gpt-5.4 (already the standard tier; gpt-5.5 is premium and
    # reserved for the CEO). Fallback demotes to the cheaper gpt-5.4-mini so an
    # error-failover never costs more than the primary.
    assert model_block["primary"] == "openai/gpt-5.4"
    assert model_block["fallbacks"] == ["openai/gpt-5.4-mini"]
    # The OPENAI_API_KEY env var is injected via ECS task secret, not in this file.
    env_block = cfg.get("env", {})
    assert "OPENAI_API_KEY" not in env_block, "API key must NEVER be embedded in openclaw.json — comes from ECS secret"


@pytest.mark.asyncio
async def test_byo_key_anthropic_branch(tmp_path):
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="byo_key",
        byo_provider="anthropic",
        user_id="u_1",
    )
    cfg = json.loads(out.read_text())
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    fallbacks = cfg["agents"]["defaults"]["model"]["fallbacks"]
    # Default tier is the cheaper Sonnet 4.6, with Haiku 4.5 as the cheap
    # error-failover. Opus 4.6 is reserved for the CEO agent (Friday) via
    # per-agent agents.list[*].model — see test_ceo_agent_gets_premium_model.
    assert primary == "anthropic/claude-sonnet-4-6"
    assert fallbacks == ["anthropic/claude-haiku-4-5"]
    # byo anthropic uses Anthropic-DIRECT ids — no Bedrock date-stamp.
    # The premium ref strips the erroneous "-v1" suffix (Bedrock-only).
    env_block = cfg.get("env", {})
    assert "ANTHROPIC_API_KEY" not in env_block


@pytest.mark.asyncio
async def test_bedrock_claude_branch(tmp_path):
    """Static catalog (no API discovery) keyed on inference-profile IDs.

    Every Claude 4.x on Bedrock is INFERENCE_PROFILE-only, so model IDs
    must carry the ``us.`` prefix; bare foundation-model IDs aren't
    invocable. Discovery is disabled — we ship exactly the models priced
    in core/billing/bedrock_pricing.py and nothing else.
    """
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        user_id="u_1",
        aip_arns=_AIPS,
    )
    cfg = json.loads(out.read_text())

    # Default tier is the cheaper Sonnet 4.6, with Haiku 4.5 as the cheap
    # error-failover. Opus 4.6 is reserved for the CEO agent (Friday) via a
    # per-agent agents.list[*].model — see test_ceo_agent_gets_premium_model.
    # Model ids are now the per-owner AIP ARNs (Plan 2), not the system
    # us.anthropic.* profiles.
    model_block = cfg["agents"]["defaults"]["model"]
    assert model_block["primary"] == f"amazon-bedrock/{_AIPS['sonnet']}"
    assert model_block["fallbacks"] == [f"amazon-bedrock/{_AIPS['haiku']}"]

    # Static provider catalog — Opus 4.6 + Sonnet 4.6 + Haiku 4.5 (the
    # cheap aux-task tier). New entries land here in lockstep with
    # bedrock_pricing._RATES; otherwise the credit ledger would either
    # skip billing or 500 with UnknownModelError. The model ids are the
    # per-owner AIP ARNs the IAM role is scoped to.
    provider = cfg["models"]["providers"]["amazon-bedrock"]
    assert provider["api"] == "bedrock-converse-stream"
    assert provider["auth"] == "aws-sdk"
    model_ids = {m["id"] for m in provider["models"]}
    assert model_ids == set(_AIPS.values())
    # Cost shape matches upstream ModelDefinitionConfig.cost (USD/MTok).
    for model in provider["models"]:
        assert set(model["cost"].keys()) == {"input", "output", "cacheRead", "cacheWrite"}
        assert model["contextWindow"] > 0

    # Discovery disabled — no bedrock:ListFoundationModels IAM needed on
    # the per-user container, and no risk of surfacing un-priced models.
    bedrock_cfg = cfg["plugins"]["entries"]["amazon-bedrock"]["config"]
    assert bedrock_cfg["discovery"]["enabled"] is False


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected",
    [
        # Anthropic-DIRECT id — no Bedrock "-v1" suffix.
        ("byo_key", "anthropic", "anthropic/claude-opus-4-6"),
        ("byo_key", "openai", "openai/gpt-5.5"),
        # chatgpt_oauth is a flat-rate plan — no premium tier to reserve, so
        # the CEO inherits the default (no per-agent model override).
        ("chatgpt_oauth", None, None),
    ],
)
def test_premium_model_ref_matrix(provider_choice, byo_provider, expected):
    """The CEO premium ref is the top-tier model per provider path, or None
    for the flat-rate chatgpt_oauth plan where there's nothing to reserve.

    The bedrock_claude row lives in test_config_aip.py
    (test_premium_and_cheap_refs_use_aip_arns) now that the ref is the
    per-owner AIP ARN rather than a hardcoded us.anthropic.* id."""
    assert premium_model_ref(provider_choice, byo_provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_ceo_model",
    [
        ("bedrock_claude", None, f"amazon-bedrock/{_AIPS['opus']}"),
        ("byo_key", "anthropic", "anthropic/claude-opus-4-6"),
        ("byo_key", "openai", "openai/gpt-5.5"),
    ],
)
async def test_ceo_agent_gets_premium_model(tmp_path, provider_choice, byo_provider, expected_ceo_model):
    """The CEO agent (role='ceo', i.e. Friday) is pinned to the premium model
    via a per-agent agents.list[*].model, while non-CEO agents carry no
    per-agent model and inherit the cheaper default."""
    out = tmp_path / "openclaw.json"
    kwargs = {
        "config_path": out,
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
        "paperclip_agents": [
            {"id": "agent_friday", "role": "ceo", "name": "Friday"},
            {"id": "agent_atlas", "role": "recruiter", "name": "Atlas"},
        ],
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads(out.read_text())

    entries = {a["id"]: a for a in cfg["agents"]["list"]}
    ceo = entries["agent_friday"]
    non_ceo = entries["agent_atlas"]
    assert ceo["default"] is True
    assert ceo["model"] == expected_ceo_model
    # Non-CEO agents have no per-agent model — they inherit the (cheaper)
    # agents.defaults.model so only Friday pays premium rates.
    assert "model" not in non_ceo


@pytest.mark.asyncio
async def test_chatgpt_oauth_ceo_inherits_default(tmp_path):
    """chatgpt_oauth is flat-rate — there's no premium tier to reserve, so
    even the CEO carries no per-agent model and inherits the default."""
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="chatgpt_oauth",
        user_id="u_1",
        paperclip_agents=[
            {"id": "agent_friday", "role": "ceo", "name": "Friday"},
            {"id": "agent_atlas", "role": "recruiter", "name": "Atlas"},
        ],
    )
    cfg = json.loads(out.read_text())
    entries = {a["id"]: a for a in cfg["agents"]["list"]}
    assert entries["agent_friday"]["default"] is True
    assert "model" not in entries["agent_friday"]
    assert "model" not in entries["agent_atlas"]


@pytest.mark.asyncio
async def test_meta_shim_present_to_defeat_auto_restore(tmp_path):
    """openclaw's io.observe-recovery flags any write missing meta as
    `missing-meta-vs-last-good` and silently restores from .bak — silently
    stripping every backend write. The synthetic meta defeats that check."""
    out = tmp_path / "openclaw.json"
    await write_openclaw_config(
        config_path=out,
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        user_id="u_1",
        aip_arns=_AIPS,
    )
    cfg = json.loads(out.read_text())
    meta = cfg.get("meta")
    assert isinstance(meta, dict), "meta must be an object"
    assert isinstance(meta.get("lastTouchedVersion"), str), (
        "meta.lastTouchedVersion (string) is what hasConfigMeta checks for"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("chatgpt_oauth", None),
        ("byo_key", "openai"),
        ("byo_key", "anthropic"),
        ("bedrock_claude", None),
    ],
)
async def test_memory_search_provider_resolves_per_auth_path(tmp_path, provider_choice, byo_provider):
    """memorySearch's embedding backend is provider-derived: semantic (vector)
    only where the owner's OWN credential pays, keyword-only (FTS) otherwise so
    Isol8 never pays for embeddings. Only byo_key+openai has a native, owner-
    billed embeddings path; chatgpt_oauth (OAuth can't hit /v1/embeddings),
    byo_key+anthropic (no embeddings API), and bedrock_claude (Titan would bill
    us, not the owner) all fall back to FTS. Recall stays enabled everywhere."""
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    memory_search = cfg["agents"]["defaults"]["memorySearch"]
    assert memory_search["enabled"] is True
    if provider_choice == "byo_key" and byo_provider == "openai":
        assert memory_search["provider"] == "openai"
        assert memory_search["model"] == "text-embedding-3-small"
    else:
        assert memory_search["provider"] == "none"
        assert "model" not in memory_search


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("bedrock_claude", None),
        ("byo_key", "anthropic"),
        ("byo_key", "openai"),
        ("chatgpt_oauth", None),
    ],
)
async def test_cache_retention_long_in_agent_defaults_params(tmp_path, provider_choice, byo_provider):
    """agents.defaults.params.cacheRetention is pinned to "long" (1h TTL).

    The Bedrock runtime defaults cacheRetention to "short" (5m TTL); the
    agent's runs are spaced further apart than that, so cached prefixes
    expire before reuse and get re-written (live metrics showed a
    write-heavy read:write ratio). "long" widens the window so re-reads
    land within it. The openai/openai providers ignore the param,
    so it's a harmless global no-op there — set unconditionally.
    """
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    assert cfg["agents"]["defaults"]["params"]["cacheRetention"] == "long"


@pytest.mark.asyncio
async def test_subagents_allow_agents_wildcard_in_defaults(tmp_path):
    """agents.defaults.subagents.allowAgents is ["*"] so every agent can see the
    full company roster via the agents_list tool.

    OpenClaw resolves agents_list from subagents.allowAgents; unset → an agent
    sees ONLY itself (agents-list-tool.ts + subagent-target-policy.ts), which
    blinds Atlas to siblings it must build out and Friday to its own team. "*"
    widens to the full configured roster. Governs the agents tool only — not
    cross-agent session visibility.
    """
    await write_openclaw_config(
        config_path=tmp_path / "openclaw.json",
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        user_id="u_1",
        aip_arns=_AIPS,
    )
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    assert cfg["agents"]["defaults"]["subagents"]["allowAgents"] == ["*"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("chatgpt_oauth", None),
        ("byo_key", "openai"),
        ("byo_key", "anthropic"),
        ("bedrock_claude", None),
    ],
)
async def test_midturn_precheck_enabled_across_auth_paths(tmp_path, provider_choice, byo_provider):
    """compaction.midTurnPrecheck.enabled is ON for every provider path.

    The precheck detects context pressure after each tool result (before the
    next model call) and truncates/compacts in-place, so a long tool-heavy
    turn recovers instead of hard-failing with "Context overflow: prompt too
    large for the model". It guards the context window, not model cost, so it
    is provider-agnostic — it must be present regardless of which provider the
    owner chose, including flat-rate chatgpt_oauth where there is no cheaper
    tier to route aux compaction to.
    """
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    compaction = cfg["agents"]["defaults"]["compaction"]
    assert compaction["midTurnPrecheck"]["enabled"] is True


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected",
    [
        # The maintenance heartbeat runs on the cheap tier for EVERY provider,
        # including chatgpt_oauth — unlike compaction, the heartbeat is an
        # ordinary turn the Codex OAuth token can serve on the mini tier.
        ("chatgpt_oauth", None, "openai/gpt-5.4-mini"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("bedrock_claude", None, f"amazon-bedrock/{_AIPS['haiku']}"),
    ],
)
def test_heartbeat_model_ref_matrix(provider_choice, byo_provider, expected):
    """heartbeat_model_ref resolves a cheap tier for every provider.

    It diverges from cheap_model_ref only on chatgpt_oauth, where the
    heartbeat (a normal turn) can use gpt-5.4-mini even though compaction
    cannot override the Codex backend's native summarizer.
    """
    assert heartbeat_model_ref(provider_choice, byo_provider, aip_arns=_AIPS) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_model",
    [
        ("chatgpt_oauth", None, "openai/gpt-5.4-mini"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("bedrock_claude", None, f"amazon-bedrock/{_AIPS['haiku']}"),
    ],
)
async def test_heartbeat_runs_on_cheap_model(tmp_path, provider_choice, byo_provider, expected_model):
    """agents.defaults.heartbeat keeps the 30m cadence but runs on the cheap
    tier for every provider — never the premium default model.

    Regression guard for chatgpt_oauth, which previously inherited the gpt-5.5
    default for the per-agent 30-min maintenance poll because cheap_model was
    None on that path.
    """
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    heartbeat = cfg["agents"]["defaults"]["heartbeat"]
    assert heartbeat["every"] == "30m"
    assert heartbeat["model"] == expected_model
    assert heartbeat["model"] != cfg["agents"]["defaults"]["model"]["primary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("chatgpt_oauth", None),
        ("byo_key", "openai"),
        ("byo_key", "anthropic"),
        ("bedrock_claude", None),
    ],
)
async def test_cron_tool_denied_across_auth_paths(tmp_path, provider_choice, byo_provider):
    """The in-container OpenClaw `cron` tool is denied on every provider path.

    Recurring/scheduled agent work is governed by Paperclip routines (the
    single scheduling authority); agents must not self-create ungoverned
    container cron jobs, so `cron` is in tools.deny.
    """
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    assert "cron" in cfg["tools"]["deny"]


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected",
    [
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        # chatgpt_oauth gets None: its Codex runtime ignores/rejects an
        # OpenClaw-side compaction summarizer override, so routing aux to
        # gpt-5.4-mini makes compaction fail and the session overflow.
        # Returning None lets aux inherit the OAuth-invocable primary (gpt-5.5).
        ("chatgpt_oauth", None, None),
    ],
)
def test_cheap_model_ref_matrix(provider_choice, byo_provider, expected):
    """The cheap aux ref is the cheapest model per provider path, routed to
    pure-overhead operations (compaction / memory-flush / heartbeat).

    The bedrock_claude row lives in test_config_aip.py
    (test_premium_and_cheap_refs_use_aip_arns) now that the ref is the
    per-owner AIP ARN rather than a hardcoded us.anthropic.* id."""
    assert cheap_model_ref(provider_choice, byo_provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_cheap",
    [
        ("bedrock_claude", None, f"amazon-bedrock/{_AIPS['haiku']}"),
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        # chatgpt_oauth deliberately excluded — it has no cheap tier (Codex
        # rejects summarizer overrides). See test_chatgpt_oauth_aux_inherits_primary.
    ],
)
async def test_aux_operations_routed_to_cheap_tier(tmp_path, provider_choice, byo_provider, expected_cheap):
    """Compaction, memory-flush, and the internal heartbeat are pure-overhead
    operations that don't need the premium default — route them to the cheap
    tier. Only the aux slots' ``.model`` is set; no enabled/mode flag is
    toggled. All three keys are valid under upstream's strict
    AgentDefaultsSchema (compaction.model, compaction.memoryFlush.model,
    heartbeat.model)."""
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    defaults = cfg["agents"]["defaults"]

    assert defaults["compaction"]["model"] == expected_cheap
    assert defaults["compaction"]["memoryFlush"]["model"] == expected_cheap
    assert defaults["heartbeat"]["model"] == expected_cheap

    # Aux routing sets ONLY .model — it must not enable a feature that wasn't
    # already on (don't flip memoryFlush.enabled). compaction.mode is pinned
    # to "safeguard" independently of the aux routing (an explicit pin of the
    # upstream new-config default, not a behavior toggle — see
    # test_config_cost_tuning).
    assert "enabled" not in defaults["compaction"].get("memoryFlush", {})
    assert defaults["compaction"]["mode"] == "safeguard"
    # The pre-existing heartbeat cadence is preserved alongside the new model.
    assert defaults["heartbeat"]["every"] == "30m"


@pytest.mark.asyncio
async def test_chatgpt_oauth_aux_inherits_primary(tmp_path):
    """chatgpt_oauth: compaction/memoryFlush must NOT be pinned to a cheap model.
    The Codex runtime ignores/rejects an OpenClaw-side summarizer override
    (gpt-5.4-mini), so pinning it made compaction fail silently (compactionCount
    stayed 0), context grew to the 272k budget, and the turn died with "Context
    overflow: prompt too large". They inherit the OAuth-invocable primary
    (gpt-5.5) so compaction runs.

    The heartbeat is the exception: it's an ordinary turn (not compaction), so it
    CAN run on the cheap tier and IS demoted to gpt-5.4-mini — keeping the 30-min
    per-agent maintenance poll off the premium model. midTurnPrecheck stays on
    (it's model-free)."""
    await write_openclaw_config(
        config_path=tmp_path / "openclaw.json",
        gateway_token="test-token",
        provider_choice="chatgpt_oauth",
        user_id="u_1",
    )
    defaults = json.loads((tmp_path / "openclaw.json").read_text())["agents"]["defaults"]

    # Compaction + memoryFlush inherit the primary (gpt-5.5) — no override.
    assert "model" not in defaults["compaction"]
    assert "model" not in defaults["compaction"].get("memoryFlush", {})
    # Heartbeat is demoted to the cheap tier (ordinary turn, not compaction).
    assert defaults["heartbeat"]["model"] == "openai/gpt-5.4-mini"
    # The context-pressure precheck stays enabled (provider-agnostic, model-free).
    assert defaults["compaction"]["midTurnPrecheck"]["enabled"] is True
    # Heartbeat cadence still present.
    assert defaults["heartbeat"]["every"] == "30m"


@pytest.mark.asyncio
async def test_byo_key_requires_byo_provider(tmp_path):
    """byo_key without byo_provider raises ValueError."""
    out = tmp_path / "openclaw.json"
    with pytest.raises(ValueError, match="byo_provider"):
        await write_openclaw_config(
            config_path=out,
            gateway_token="test-token",
            provider_choice="byo_key",
            user_id="u_1",
        )


@pytest.mark.asyncio
async def test_unknown_provider_choice_raises(tmp_path):
    out = tmp_path / "openclaw.json"
    with pytest.raises(ValueError, match="Unknown provider_choice"):
        await write_openclaw_config(
            config_path=out,
            gateway_token="test-token",
            provider_choice="totally_made_up",
            user_id="u_1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", ["", None])
async def test_empty_gateway_token_raises(tmp_path, bad_token):
    """Empty/None gateway_token must fail fast — never silently write a null auth."""
    out = tmp_path / "openclaw.json"
    with pytest.raises(ValueError, match="gateway_token must be a non-empty string"):
        await write_openclaw_config(
            config_path=out,
            gateway_token=bad_token,
            provider_choice="bedrock_claude",
            user_id="u_1",
        )


def _bedrock_catalog_models() -> list[dict]:
    providers, _default, _plugins = build_provider_block(
        provider_choice="bedrock_claude",
        user_id="u_1",
        byo_provider=None,
        aip_arns=_AIPS,
    )
    return providers["amazon-bedrock"]["models"]


def test_bedrock_catalog_ids_are_the_known_valid_set():
    """The catalog ids must equal the per-owner AIP ARN set threaded through
    the builder. With aip_arns supplied, each model id is the owner's AIP
    ARN (Plan 2), not the system us.anthropic.* profile."""
    ids = {m["id"] for m in _bedrock_catalog_models()}
    assert ids == _VALID_BEDROCK_IDS


# With per-owner AIP ARNs threaded in, the catalog model ids are opaque AIP
# ARNs (Plan 2), not the us.anthropic.* profile ids the rate table keys on.
# The catalog order is fixed (opus, sonnet, haiku) and each AIP ARN maps back
# to a tier via _AIPS; this maps the catalog id -> the bare foundation-model
# rate-table key so the cost-block drift check still works.
_AIP_ARN_TO_RATE_KEY = {
    _AIPS["opus"]: "anthropic.claude-opus-4-6-v1",
    _AIPS["sonnet"]: "anthropic.claude-sonnet-4-6",
    _AIPS["haiku"]: "anthropic.claude-haiku-4-5-20251001-v1:0",
}


def test_config_cost_blocks_match_bedrock_pricing_rates():
    """config.py cost blocks and bedrock_pricing._RATES must agree per model
    so the two rate tables can't drift. Maps camelCase ↔ snake_case keys.

    The model id is now the per-owner AIP ARN (Plan 2), which is opaque to the
    rate table, so the AIP ARN is mapped back to the bare foundation-model
    rate-table key before lookup. The cost blocks themselves are unchanged
    (still the per-tier values keyed in the builder)."""
    for model in _bedrock_catalog_models():
        rate_key = _AIP_ARN_TO_RATE_KEY[model["id"]]
        rate = get_rate(rate_key)
        for camel, snake in _COST_KEY_MAP.items():
            assert model["cost"][camel] == rate[snake], (
                f"{rate_key}: cost.{camel}={model['cost'][camel]} != _RATES[{snake}]={rate[snake]}"
            )


# =============================================================================
# Bedrock TRIAL config mode (one tier + lockdown) and the runaway bounds
# =============================================================================

# A trial owner's IAM role is pinned to a SINGLE AIP (sonnet) — minting one
# tier pins them to one model, so the emitted catalog must carry exactly that
# one model or every chat AccessDenies.
_TRIAL_AIPS = {"sonnet": _AIPS["sonnet"]}

_SONNET_REF = f"amazon-bedrock/{_AIPS['sonnet']}"

_PAPERCLIP_AGENTS = [
    {"id": "agent_friday", "role": "ceo", "name": "Friday"},
    {"id": "agent_atlas", "role": "general", "name": "Atlas"},
]

_CHANNEL_ACCOUNTS = {
    "telegram": {"acct_1": {"token": "tg-token", "allowFrom": ["12345"]}},
}


def _trial_cfg(**kwargs) -> dict:
    defaults = {
        "user_id": "u_trial",
        "gateway_token": "test-token",
        "provider_choice": "bedrock_trial",
        "aip_arns": _TRIAL_AIPS,
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


def _paid_cfg(**kwargs) -> dict:
    defaults = {
        "user_id": "u_paid",
        "gateway_token": "test-token",
        "provider_choice": "bedrock_claude",
        "aip_arns": _AIPS,
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


# --- (a) one-tier shape -------------------------------------------------------


def test_bedrock_trial_emits_exactly_one_model():
    providers, default_model, plugin_entries = build_provider_block(
        provider_choice="bedrock_trial",
        user_id="u_trial",
        byo_provider=None,
        aip_arns=_TRIAL_AIPS,
    )
    models = providers["amazon-bedrock"]["models"]
    assert [m["id"] for m in models] == [_AIPS["sonnet"]]
    assert models[0]["name"] == "Claude Sonnet 4.6"
    assert models[0]["cost"] == {"input": 3.3, "output": 16.5, "cacheRead": 0.33, "cacheWrite": 6.6}
    # Primary == fallback: there is no cheaper/premium tier to route to, and
    # the single model must not be user-swappable to one IAM would deny.
    assert default_model == {"primary": _SONNET_REF, "fallbacks": [_SONNET_REF]}
    # Discovery stays off — same as the paid path.
    assert plugin_entries["amazon-bedrock"]["config"]["discovery"]["enabled"] is False


def test_bedrock_trial_has_no_premium_or_cheap_ladder():
    assert premium_model_ref("bedrock_trial", None, aip_arns=_TRIAL_AIPS) is None
    assert cheap_model_ref("bedrock_trial", None, aip_arns=_TRIAL_AIPS) is None
    assert heartbeat_model_ref("bedrock_trial", None, aip_arns=_TRIAL_AIPS) is None


def test_bedrock_trial_ceo_inherits_the_single_default_model():
    """No opus tier exists, so Friday must NOT carry a per-agent model override."""
    cfg = _trial_cfg(paperclip_agents=_PAPERCLIP_AGENTS)
    friday = next(a for a in cfg["agents"]["list"] if a["id"] == "agent_friday")
    assert friday["default"] is True
    assert "model" not in friday
    assert cfg["agents"]["defaults"]["model"]["primary"] == _SONNET_REF


def test_bedrock_trial_aux_operations_inherit_the_single_model():
    """No haiku tier — compaction/memoryFlush must not pin a model the owner's
    IAM policy would deny."""
    compaction = _trial_cfg()["agents"]["defaults"]["compaction"]
    assert compaction["midTurnPrecheck"] == {"enabled": True}
    assert "model" not in compaction
    assert "memoryFlush" not in compaction


def test_one_aip_bedrock_claude_no_longer_raises():
    """The hard-fail on a partial AIP map is gone: emit exactly the tiers the
    owner's IAM actually permits. (Policy lockdown is keyed on the trial
    provider mode, NOT on the tier count — a one-tier bedrock_claude owner
    keeps the paid policy.)"""
    cfg = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        aip_arns=_TRIAL_AIPS,
    )
    models = cfg["models"]["providers"]["amazon-bedrock"]["models"]
    assert [m["id"] for m in models] == [_AIPS["sonnet"]]
    assert cfg["agents"]["defaults"]["model"] == {"primary": _SONNET_REF, "fallbacks": [_SONNET_REF]}
    assert "cron" not in cfg
    assert cfg["agents"]["defaults"]["heartbeat"]["every"] == "30m"


@pytest.mark.parametrize("provider_choice", ["bedrock_claude", "bedrock_trial"])
@pytest.mark.parametrize("bad_aips", [None, {}, {"opus": _AIPS["opus"], "haiku": _AIPS["haiku"]}])
def test_bedrock_without_sonnet_aip_fails_closed(provider_choice, bad_aips):
    """Sonnet is the default model on every bedrock path — no sonnet AIP means
    the container could not invoke anything. Raise loud at provision."""
    with pytest.raises(ValueError, match="aip_arns"):
        build_openclaw_config_dict(
            user_id="u_1",
            gateway_token="test-token",
            provider_choice=provider_choice,
            aip_arns=bad_aips,
        )


# --- (b) trial lockdown (re-homed from the deleted meta branch) ---------------


def test_bedrock_trial_disables_cron_service():
    assert _trial_cfg()["cron"] == {"enabled": False}


def test_bedrock_trial_omits_heartbeat():
    """A present `heartbeat` key is truthy to OpenClaw regardless of contents,
    so the only way to stop the recurring self-run is to omit it."""
    assert "heartbeat" not in _trial_cfg()["agents"]["defaults"]


def test_bedrock_trial_skips_channel_account_reinjection():
    cfg = _trial_cfg(channel_accounts=_CHANNEL_ACCOUNTS)
    assert cfg["channels"] == {}
    assert "bindings" not in cfg


def test_bedrock_trial_emits_no_acp_block():
    """ACP harnesses (Claude Code via Bedrock) invoke model ids the one-tier
    AIP policy denies — and are an autonomy surface a trial must not get."""
    cfg = _trial_cfg(acp_enabled=True)
    assert "acp" not in cfg
    assert "acpx" not in cfg["plugins"]["entries"]


def test_paid_bedrock_keeps_cron_heartbeat_channels_and_acp():
    """The trial lockdown must not leak onto the paid path."""
    cfg = _paid_cfg(channel_accounts=_CHANNEL_ACCOUNTS, acp_enabled=True)
    assert "cron" not in cfg
    assert cfg["agents"]["defaults"]["heartbeat"] == {
        "every": "30m",
        "isolatedSession": True,
        "lightContext": True,
        "model": f"amazon-bedrock/{_AIPS['haiku']}",
    }
    assert cfg["channels"]["telegram"]["enabled"] is True
    assert cfg["channels"]["telegram"]["accounts"] == _CHANNEL_ACCOUNTS["telegram"]
    assert cfg["bindings"] == [
        {"type": "route", "agentId": "acct_1", "match": {"channel": "telegram", "accountId": "acct_1"}}
    ]
    assert cfg["acp"]["allowedAgents"] == ["claude"]


def test_bedrock_trial_omits_skill_workshop_autocapture():
    """Autonomous skill capture runs an extra model review over qualifying
    turns — the $3 trial budget must fund the user's own messages, not
    background self-improvement."""
    assert "workshop" not in _trial_cfg()["skills"]


@pytest.mark.parametrize(
    "provider_choice,byo_provider",
    [
        ("chatgpt_oauth", None),
        ("byo_key", "openai"),
        ("byo_key", "anthropic"),
        ("bedrock_claude", None),
    ],
)
def test_paid_paths_enable_skill_workshop_autocapture(provider_choice, byo_provider):
    """Every paid provider path gets autonomous Skill Workshop capture:
    durable corrections and proven workflows become pending skill proposals
    (reviewed in the Skill Proposals panel — the autonomous path never
    self-applies)."""
    kwargs = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "aip_arns": _AIPS,
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    cfg = build_openclaw_config_dict(**kwargs)
    assert cfg["skills"]["workshop"] == {"autonomous": {"enabled": True}}


def test_paid_bedrock_three_tier_ladder_is_unchanged():
    """Byte-identical paid ladder: catalog order, primary/fallback, CEO premium
    pin, cheap-tier aux routing."""
    cfg = _paid_cfg(paperclip_agents=_PAPERCLIP_AGENTS)
    models = cfg["models"]["providers"]["amazon-bedrock"]["models"]
    assert [m["id"] for m in models] == [_AIPS["opus"], _AIPS["sonnet"], _AIPS["haiku"]]
    assert cfg["agents"]["defaults"]["model"] == {
        "primary": _SONNET_REF,
        "fallbacks": [f"amazon-bedrock/{_AIPS['haiku']}"],
    }
    friday = next(a for a in cfg["agents"]["list"] if a["id"] == "agent_friday")
    assert friday["model"] == f"amazon-bedrock/{_AIPS['opus']}"
    compaction = cfg["agents"]["defaults"]["compaction"]
    assert compaction["model"] == f"amazon-bedrock/{_AIPS['haiku']}"
    assert compaction["memoryFlush"] == {"model": f"amazon-bedrock/{_AIPS['haiku']}"}


# --- (c) runaway bounds — asserted on BOTH the trial and the paid path --------

_BOUNDED_CONFIGS = [
    ("bedrock_trial", {"aip_arns": _TRIAL_AIPS}),
    ("bedrock_claude", {"aip_arns": _AIPS}),
    ("chatgpt_oauth", {}),
    ("byo_key", {"byo_provider": "anthropic"}),
    ("byo_key", {"byo_provider": "openai"}),
]


@pytest.mark.parametrize("provider_choice,kwargs", _BOUNDED_CONFIGS)
def test_agent_run_deadline_is_thirty_minutes(provider_choice, kwargs):
    """agents.defaults.timeoutSeconds defaults to 172800 (48h) upstream, and the
    agentic loop has no step/tool-call/token cap — this wall clock is the ONLY
    thing that stops a runaway run."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert cfg["agents"]["defaults"]["timeoutSeconds"] == 1800


@pytest.mark.parametrize("provider_choice,kwargs", _BOUNDED_CONFIGS)
def test_run_retries_capped(provider_choice, kwargs):
    """Upstream default is 160 retries, each of which re-arms the deadline."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert cfg["agents"]["defaults"]["runRetries"] == {"max": 8}


def test_subagent_concurrency_capped_on_trial_only():
    """Subagents do NOT count against agents.defaults.maxConcurrent, so the
    fan-out lane needs its own bound (upstream default 8) — but only where the
    spend is OURS.

    Unlike the other three bounds (timeoutSeconds, runRetries, loopDetection),
    which are sane everywhere because no legitimate run wants 48 hours or 160
    retries, this one throttles CAPABILITY. A paid owner's spend is bounded by
    their own credits or their own API key, so there is no reason to make them
    delegate more slowly than upstream allows."""
    trial = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="test-token",
        provider_choice="bedrock_trial",
        aip_arns={"sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/s"},
    )
    assert trial["agents"]["defaults"]["subagents"]["maxConcurrent"] == 2
    # The roster-visibility wildcard must survive alongside the bound.
    assert trial["agents"]["defaults"]["subagents"]["allowAgents"] == ["*"]

    paid = build_openclaw_config_dict(
        user_id="u_1",
        gateway_token="test-token",
        provider_choice="bedrock_claude",
        aip_arns={
            "sonnet": "arn:aws:bedrock:us-east-1:1:application-inference-profile/s",
            "opus": "arn:aws:bedrock:us-east-1:1:application-inference-profile/o",
            "haiku": "arn:aws:bedrock:us-east-1:1:application-inference-profile/h",
        },
    )
    assert "maxConcurrent" not in paid["agents"]["defaults"]["subagents"]
    assert paid["agents"]["defaults"]["subagents"]["allowAgents"] == ["*"]


@pytest.mark.parametrize("provider_choice,kwargs", _BOUNDED_CONFIGS)
def test_tool_loop_detection_enabled(provider_choice, kwargs):
    """Upstream's own circuit breaker, which defaults to OFF."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert cfg["tools"]["loopDetection"] == {"enabled": True}


@pytest.mark.parametrize("provider_choice,kwargs", _BOUNDED_CONFIGS)
def test_global_agent_semaphore_and_cache_retention_untouched(provider_choice, kwargs):
    """agents.defaults.maxConcurrent stays at its upstream default of 4, and
    cacheRetention stays "long" — "none" would kill cache READS too and make a
    long loop dramatically MORE expensive."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert "maxConcurrent" not in cfg["agents"]["defaults"]
    assert cfg["agents"]["defaults"]["params"]["cacheRetention"] == "long"


@pytest.mark.parametrize("provider_choice,kwargs", [_BOUNDED_CONFIGS[0], _BOUNDED_CONFIGS[1]])
def test_bedrock_context_tokens_bounded(provider_choice, kwargs):
    """The Bedrock catalog declares a 1M context window, so a single call could
    take ~$6.74 of input. Bound the effective budget (and trigger compaction
    earlier)."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert cfg["agents"]["defaults"]["contextTokens"] == 200_000


@pytest.mark.parametrize("provider_choice,kwargs", _BOUNDED_CONFIGS[2:])
def test_context_tokens_unset_off_bedrock(provider_choice, kwargs):
    """contextTokens is derived from the Bedrock catalog's declared window; the
    ChatGPT/Codex path serves ~272k and must keep inheriting its own budget."""
    cfg = build_openclaw_config_dict(
        user_id="u_1", gateway_token="test-token", provider_choice=provider_choice, **kwargs
    )
    assert "contextTokens" not in cfg["agents"]["defaults"]
