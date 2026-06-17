"""ACP coding-harness runtime: derived harnesses + gated openclaw.json emission."""

import pytest

from core.containers.config import (
    build_acp_config,
    build_openclaw_config_dict,
    derive_enabled_harnesses,
)

_TOKEN = "tok-acp-test"


# ---- derive_enabled_harnesses (pure, no stored state) ----


@pytest.mark.parametrize(
    "provider_choice,byo_provider,expected",
    [
        ("bedrock_claude", None, ["claude"]),
        ("chatgpt_oauth", None, ["codex"]),
        ("byo_key", "anthropic", ["claude"]),
        ("byo_key", "openai", ["codex"]),
        # No Google credential path today -> no harness; unknown combos -> none.
        ("byo_key", "google", []),
        ("byo_key", None, []),
        ("nonsense", None, []),
    ],
)
def test_derive_enabled_harnesses(provider_choice, byo_provider, expected):
    assert derive_enabled_harnesses(provider_choice, byo_provider) == expected


# ---- build_acp_config ----


def test_build_acp_config_shape():
    acp_block, acpx_entry = build_acp_config(["claude"])
    assert acp_block["enabled"] is True
    assert acp_block["backend"] == "acpx"
    assert acp_block["allowedAgents"] == ["claude"]
    assert acp_block["defaultAgent"] == "claude"
    assert acpx_entry["acpx"]["enabled"] is True
    # Non-interactive container: auto-approve, degrade (don't crash) on prompt.
    cfg = acpx_entry["acpx"]["config"]
    assert cfg["permissionMode"] == "approve-all"
    assert cfg["nonInteractivePermissions"] == "deny"


def test_build_acp_config_copies_harness_list():
    """allowedAgents must not alias the caller's list."""
    harnesses = ["codex"]
    acp_block, _ = build_acp_config(harnesses)
    harnesses.append("claude")
    assert acp_block["allowedAgents"] == ["codex"]


# ---- gated emission in build_openclaw_config_dict ----


def _build(provider_choice, byo_provider=None, *, acp_enabled):
    return build_openclaw_config_dict(
        user_id="user_acp",
        gateway_token=_TOKEN,
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        acp_enabled=acp_enabled,
    )


def test_acp_omitted_by_default():
    """Dark by default: no acp block, no acpx entry when acp_enabled is False."""
    config = _build("chatgpt_oauth", acp_enabled=False)
    assert "acp" not in config
    assert "acpx" not in config["plugins"]["entries"]


def test_acp_emitted_when_enabled_chatgpt():
    config = _build("chatgpt_oauth", acp_enabled=True)
    assert config["acp"]["allowedAgents"] == ["codex"]
    assert config["plugins"]["entries"]["acpx"]["enabled"] is True


def test_acp_emitted_when_enabled_byo_anthropic():
    config = _build("byo_key", "anthropic", acp_enabled=True)
    assert config["acp"]["allowedAgents"] == ["claude"]
    assert "acpx" in config["plugins"]["entries"]
