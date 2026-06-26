"""Tests for the curated chat ``commands`` flag block in openclaw.json.

The per-user container exposes a handful of privileged chat slash-commands.
The slash-command menu lives in the GUI control panel, which owns MCP /
plugins / config writes — so those must NOT be mutable from chat. Host-shell
(/bash) and gateway /restart widen the trust boundary too far for chat too.
"""

from core.containers.config import build_openclaw_config_dict


_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def _cfg(provider_choice: str = "bedrock_claude", **kwargs) -> dict:
    defaults = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "aip_arns": _AIPS,
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


def test_commands_block_is_curated():
    commands = _cfg()["commands"]
    # Privileged surfaces the GUI owns must NOT be enabled from chat.
    assert "mcp" not in commands or commands["mcp"] is False
    assert "plugins" not in commands or commands["plugins"] is False
    # Host/lifecycle footguns stay off.
    assert commands.get("restart") is False
    assert "bash" not in commands or commands["bash"] is False
    assert "config" not in commands or commands["config"] is False
    # debug stays enabled (runtime-only, not persisted).
    assert commands.get("debug") is True
