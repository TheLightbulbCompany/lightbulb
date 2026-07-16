"""Tests for per-owner AIP-ARN threading into the openclaw.json builder."""

import pytest

from core.containers.config import (
    build_openclaw_config_dict,
    build_provider_block,
    cheap_model_ref,
    premium_model_ref,
)

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def test_provider_block_uses_aip_arns_as_model_ids():
    providers, default_model, _plugins = build_provider_block(
        provider_choice="bedrock_claude",
        user_id="org_abc",
        byo_provider=None,
        aip_arns=_AIPS,
    )
    model_ids = {m["id"] for m in providers["amazon-bedrock"]["models"]}
    assert model_ids == set(_AIPS.values())
    assert default_model["primary"] == f"amazon-bedrock/{_AIPS['sonnet']}"
    assert default_model["fallbacks"] == [f"amazon-bedrock/{_AIPS['haiku']}"]


def test_provider_block_bedrock_without_aips_raises():
    with pytest.raises(ValueError, match="aip_arns"):
        build_provider_block(
            provider_choice="bedrock_claude",
            user_id="org_abc",
            byo_provider=None,
            aip_arns=None,
        )


def test_premium_and_cheap_refs_use_aip_arns():
    assert premium_model_ref("bedrock_claude", None, aip_arns=_AIPS) == f"amazon-bedrock/{_AIPS['opus']}"
    assert cheap_model_ref("bedrock_claude", None, aip_arns=_AIPS) == f"amazon-bedrock/{_AIPS['haiku']}"


def _bedrock_base_url(via_gateway: bool) -> str:
    providers, _default, _plugins = build_provider_block(
        provider_choice="bedrock_claude",
        user_id="org_abc",
        byo_provider=None,
        aip_arns=_AIPS,
        via_gateway=via_gateway,
    )
    return providers["amazon-bedrock"]["baseUrl"]


def test_bedrock_baseurl_is_direct_endpoint_by_default():
    # via_gateway defaults False -> direct bedrock-runtime endpoint (today).
    assert _bedrock_base_url(via_gateway=False).startswith("https://bedrock-runtime.")


def test_bedrock_baseurl_swaps_to_gate_only_when_flag_and_url_set(monkeypatch):
    monkeypatch.setattr("core.containers.config.settings.BEDROCK_GATE_URL", "https://gate.dev.example/")
    # Flag on + URL set -> gate URL.
    assert _bedrock_base_url(via_gateway=True) == "https://gate.dev.example/"
    # Flag OFF (URL set) -> still direct.
    assert _bedrock_base_url(via_gateway=False).startswith("https://bedrock-runtime.")


def test_bedrock_baseurl_stays_direct_when_flag_on_but_url_empty(monkeypatch):
    monkeypatch.setattr("core.containers.config.settings.BEDROCK_GATE_URL", "")
    # Flag on but no gate URL configured -> direct (flag flip is inert).
    assert _bedrock_base_url(via_gateway=True).startswith("https://bedrock-runtime.")


def test_build_config_dict_threads_via_gateway_into_provider_baseurl(monkeypatch):
    monkeypatch.setattr("core.containers.config.settings.BEDROCK_GATE_URL", "https://gate.dev.example/")
    config = build_openclaw_config_dict(
        user_id="org_abc",
        gateway_token="gw",
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        via_gateway=True,
    )
    assert config["models"]["providers"]["amazon-bedrock"]["baseUrl"] == "https://gate.dev.example/"
    # auth is unchanged — the bearer env overrides the scheme in the plugin.
    assert config["models"]["providers"]["amazon-bedrock"]["auth"] == "aws-sdk"


def test_byo_path_ignores_aip_arns():
    # A non-bedrock provider must not require aip_arns.
    providers, default_model, _ = build_provider_block(
        provider_choice="byo_key",
        user_id="org_abc",
        byo_provider="anthropic",
        aip_arns=None,
    )
    assert default_model["primary"] == "anthropic/claude-sonnet-4-6"


def test_build_config_dict_bedrock_without_aips_raises():
    with pytest.raises(ValueError, match="aip_arns"):
        build_openclaw_config_dict(
            user_id="org_abc",
            gateway_token="tok",
            provider_choice="bedrock_claude",
            aip_arns=None,
        )


def test_build_config_dict_bedrock_with_aips_emits_arn_default():
    config = build_openclaw_config_dict(
        user_id="org_abc",
        gateway_token="tok",
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
    )
    assert config["agents"]["defaults"]["model"]["primary"] == f"amazon-bedrock/{_AIPS['sonnet']}"
