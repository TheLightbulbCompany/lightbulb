"""write_openclaw_config emits the correct provider block per provider_choice."""

import json

import pytest

from core.billing.bedrock_pricing import get_rate, normalize_model_id
from core.containers.config import (
    build_provider_block,
    cheap_model_ref,
    premium_model_ref,
    write_openclaw_config,
)

# The known-valid Bedrock inference-profile ids. Pins what the catalog emits
# so a bad id (e.g. the bare us.anthropic.claude-haiku-4-5, which is NOT a
# valid profile and throws ValidationException on invoke) can't slip back in.
# Live list-inference-profiles validation lands in a later task; this just
# locks the expected set.
_VALID_BEDROCK_IDS = {
    "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}

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
    assert primary == "openai-codex/gpt-5.5"
    # OpenClaw's openai-codex provider has no JSON config knob for the
    # auth dir — it reads the CODEX_HOME env var (set on the per-user ECS
    # task in ecs_manager). The provider block is omitted entirely so the
    # base schema validator (which requires baseUrl + models on a populated
    # entry) doesn't reject the empty `{}` we'd otherwise emit.
    assert "openai-codex" not in cfg["models"].get("providers", {})


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
    )
    cfg = json.loads(out.read_text())

    # Default tier is the cheaper Sonnet 4.6, with Haiku 4.5 as the cheap
    # error-failover. Opus 4.6 is reserved for the CEO agent (Friday) via a
    # per-agent agents.list[*].model — see test_ceo_agent_gets_premium_model.
    # Opus 4.7 is intentionally absent from the catalog: it ships with
    # applied=0 TPM on most accounts (Service Quotas L-5DB28B7B), which
    # throttles the very first invocation.
    model_block = cfg["agents"]["defaults"]["model"]
    assert model_block["primary"] == "amazon-bedrock/us.anthropic.claude-sonnet-4-6"
    assert model_block["fallbacks"] == ["amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"]

    # Static provider catalog — Opus 4.6 + Sonnet 4.6 + Haiku 4.5 (the
    # cheap aux-task tier). New entries land here in lockstep with
    # bedrock_pricing._RATES; otherwise the credit ledger would either
    # skip billing or 500 with UnknownModelError. Haiku carries the
    # date-stamped inference-profile id — the bare us.anthropic.claude-haiku-4-5
    # is NOT a valid Bedrock profile and throws ValidationException on invoke.
    provider = cfg["models"]["providers"]["amazon-bedrock"]
    assert provider["api"] == "bedrock-converse-stream"
    assert provider["auth"] == "aws-sdk"
    model_ids = {m["id"] for m in provider["models"]}
    assert model_ids == {
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    }
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
        ("bedrock_claude", None, "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"),
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
    for the flat-rate chatgpt_oauth plan where there's nothing to reserve."""
    assert premium_model_ref(provider_choice, byo_provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_ceo_model",
    [
        ("bedrock_claude", None, "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"),
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
async def test_memory_search_pinned_to_bedrock_across_auth_paths(tmp_path, provider_choice, byo_provider):
    """memorySearch pin defends against openclaw defaulting to "local"
    (a GGUF file we don't ship). Today's `builtin` memory backend doesn't
    use embeddings, but future plugins will inherit this default."""
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    memory_search = cfg["agents"]["defaults"]["memorySearch"]
    assert memory_search["provider"] == "bedrock"
    assert memory_search["model"] == "amazon.titan-embed-text-v2:0"


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
    land within it. The openai/openai-codex providers ignore the param,
    so it's a harmless global no-op there — set unconditionally.
    """
    kwargs = {
        "config_path": tmp_path / "openclaw.json",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "user_id": "u_1",
    }
    if byo_provider is not None:
        kwargs["byo_provider"] = byo_provider
    await write_openclaw_config(**kwargs)
    cfg = json.loads((tmp_path / "openclaw.json").read_text())
    assert cfg["agents"]["defaults"]["params"]["cacheRetention"] == "long"


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected",
    [
        ("bedrock_claude", None, "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        # chatgpt_oauth is flat-rate, but the codex provider DOES carry a
        # mini tier (gpt-5.4-mini, in the openai-codex catalog) — route aux
        # work to it for rate-limit headroom on the flat-rate plan.
        ("chatgpt_oauth", None, "openai-codex/gpt-5.4-mini"),
    ],
)
def test_cheap_model_ref_matrix(provider_choice, byo_provider, expected):
    """The cheap aux ref is the cheapest model per provider path, routed to
    pure-overhead operations (compaction / memory-flush / heartbeat)."""
    assert cheap_model_ref(provider_choice, byo_provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected_cheap",
    [
        ("bedrock_claude", None, "amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("byo_key", "anthropic", "anthropic/claude-haiku-4-5"),
        ("byo_key", "openai", "openai/gpt-5.4-mini"),
        ("chatgpt_oauth", None, "openai-codex/gpt-5.4-mini"),
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
    # already on (don't flip memoryFlush.enabled or any compaction toggle).
    assert "enabled" not in defaults["compaction"].get("memoryFlush", {})
    assert "mode" not in defaults["compaction"]
    # The pre-existing heartbeat cadence is preserved alongside the new model.
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
    )
    return providers["amazon-bedrock"]["models"]


def test_bedrock_catalog_ids_are_the_known_valid_set():
    """The catalog ids must equal the known-valid Bedrock inference-profile
    set. The bare us.anthropic.claude-haiku-4-5 is NOT a valid profile — it
    throws ValidationException on invoke, breaking the Sonnet→Haiku fallback
    and every aux op (compaction/heartbeat/memoryFlush)."""
    ids = {m["id"] for m in _bedrock_catalog_models()}
    assert ids == _VALID_BEDROCK_IDS


def test_config_cost_blocks_match_bedrock_pricing_rates():
    """config.py cost blocks and bedrock_pricing._RATES must agree per model
    so the two rate tables can't drift. Maps camelCase ↔ snake_case keys and
    normalizes the catalog id to the bare foundation-model key the rate table
    uses."""
    for model in _bedrock_catalog_models():
        bare = normalize_model_id(model["id"])
        rate = get_rate(model["id"])
        for camel, snake in _COST_KEY_MAP.items():
            assert model["cost"][camel] == rate[snake], (
                f"{bare}: cost.{camel}={model['cost'][camel]} != _RATES[{snake}]={rate[snake]}"
            )
