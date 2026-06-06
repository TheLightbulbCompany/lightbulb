"""write_openclaw_config emits the correct provider block per provider_choice."""

import json

import pytest

from core.containers.config import write_openclaw_config


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
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "openai/gpt-5.4"
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
    assert primary == "anthropic/claude-opus-4-6-v1"
    assert fallbacks == ["anthropic/claude-sonnet-4-6"]
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

    # Primary is Opus 4.6 — Opus 4.7 ships with applied=0 TPM on most
    # accounts pending AWS capacity rollout (Service Quotas L-5DB28B7B),
    # which throttles the very first invocation. 4.6 is functionally
    # identical at the same list price. 4.7 is intentionally omitted
    # from the catalog until the quota lifts; add it back as a fallback
    # (or promote to primary) at that point. Sonnet 4.6 fallback is
    # ~5× cheaper escape hatch for capacity/throttling.
    model_block = cfg["agents"]["defaults"]["model"]
    assert model_block["primary"] == "amazon-bedrock/us.anthropic.claude-opus-4-6-v1"
    assert model_block["fallbacks"] == ["amazon-bedrock/us.anthropic.claude-sonnet-4-6"]

    # Static provider catalog — Opus 4.6 + Sonnet 4.6 + Haiku 4.5 (the
    # cheap aux-task tier). New entries land here in lockstep with
    # bedrock_pricing._RATES; otherwise the credit ledger would either
    # skip billing or 500 with UnknownModelError.
    provider = cfg["models"]["providers"]["amazon-bedrock"]
    assert provider["api"] == "bedrock-converse-stream"
    assert provider["auth"] == "aws-sdk"
    model_ids = {m["id"] for m in provider["models"]}
    assert model_ids == {
        "us.anthropic.claude-opus-4-6-v1",
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-haiku-4-5",
    }
    # Cost shape matches upstream ModelDefinitionConfig.cost (USD/MTok).
    for model in provider["models"]:
        assert set(model["cost"].keys()) == {"input", "output", "cacheRead", "cacheWrite"}
        assert model["contextWindow"] > 0

    # Discovery disabled — no bedrock:ListFoundationModels IAM needed on
    # the per-user container, and no risk of surfacing un-priced models.
    bedrock_cfg = cfg["plugins"]["entries"]["amazon-bedrock"]["config"]
    assert bedrock_cfg["discovery"]["enabled"] is False


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
