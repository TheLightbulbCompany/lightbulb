"""The baked marketplace plugin must be enabled on every provider path.

Its tools are how an agent discovers hireable agents instead of waving at a
URL; a provider path that silently omits the plugin gets the old behaviour
back with no signal.
"""

import pytest

from core.containers.config import build_openclaw_config_dict

_AIPS = {
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}

_PROVIDER_PATHS = ["bedrock_claude", "bedrock_trial", "chatgpt_oauth", "byo_key"]


def _cfg(provider_choice: str, **kwargs) -> dict:
    defaults = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "aip_arns": _AIPS,
    }
    if provider_choice == "byo_key":
        defaults["byo_provider"] = "anthropic"
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


@pytest.mark.parametrize("provider_choice", _PROVIDER_PATHS)
def test_marketplace_plugin_enabled_on_every_provider_path(provider_choice):
    config = _cfg(provider_choice)
    assert config["plugins"]["entries"]["isol8-marketplace"] == {"enabled": True}


@pytest.mark.parametrize("provider_choice", _PROVIDER_PATHS)
def test_marketplace_tools_are_not_denied(provider_choice):
    """tools.deny wins over the tools.allow wildcard, so a stray denial would
    silently remove the tools while leaving the plugin 'enabled'."""
    deny = _cfg(provider_choice)["tools"]["deny"]
    assert "marketplace_search" not in deny
    assert "recommend_agents" not in deny


def test_tools_allow_wildcard_is_present():
    """The explicit ["*"] is what surfaces plugin tools at all — profile 'full'
    alone does not (see the optional-plugin-tool gate)."""
    assert _cfg("bedrock_claude")["tools"]["allow"] == ["*"]
