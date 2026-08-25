"""Both sides of the 2026.8.1 config-shape gate.

``build_openclaw_config_dict`` emits the shape the image THIS environment has
actually deployed, not the one we are building toward. That matters because the
two failure modes are asymmetric and both are silent-ish:

* 8.1 shape written to a 7.2 container: the container boots fine, but keys it
  still honours are simply gone -- the audit ledger comes back on (the EFS
  SQLite lock contender we disabled deliberately), the hooks body cap falls back
  to 64KB, runRetries goes back to upstream's 160.
* 7.2 shape written to an 8.1 container: the root schema is strict, so the
  retired keys fail validation and the gateway refuses to start.

The gate flips per environment as each tag is promoted, so during the rollout
one environment is on each side of it. These tests pin both.
"""

import pytest

from core import openclaw_version
from core.containers.config import build_openclaw_config_dict
from core.services import config_schema

_COMMON = {"user_id": "u_1", "gateway_token": "t" * 32, "provider_choice": "chatgpt_oauth"}

# Keys 2026.8.1 retired that a 7.2 container still honours.
_LEGACY_ROOT = ("audit", "web")


def _cfg(monkeypatch, *, emit_81: bool) -> dict:
    monkeypatch.setattr(openclaw_version, "deployed_upstream_at_least", lambda target: emit_81)
    return build_openclaw_config_dict(**_COMMON)


def test_81_shape_validates_against_the_81_schema(monkeypatch):
    cfg = _cfg(monkeypatch, emit_81=True)
    errors = config_schema.schema_errors(cfg)
    assert errors == [], errors


def test_81_shape_drops_every_retired_key(monkeypatch):
    cfg = _cfg(monkeypatch, emit_81=True)
    for key in _LEGACY_ROOT:
        assert key not in cfg, key
    assert "pricing" not in cfg["models"]
    assert "backend" not in cfg["memory"]
    assert "store" not in cfg["cron"]
    assert "maxBodyBytes" not in cfg["hooks"]
    assert "allowCommands" not in cfg["gateway"]["nodes"]
    defaults = cfg["agents"]["defaults"]
    assert "runRetries" not in defaults
    assert "memorySearch" not in defaults
    assert "contextTokens" not in defaults


def test_72_shape_still_emits_what_a_72_container_honours(monkeypatch):
    """The rollout window: prod is still on 7.2 while dev is on 8.1. Losing
    these on prod is a real degradation, not a cosmetic diff."""
    cfg = _cfg(monkeypatch, emit_81=False)
    for key in _LEGACY_ROOT:
        assert key in cfg, key
    assert cfg["audit"] == {"enabled": False}
    assert cfg["models"]["pricing"] == {"enabled": False}
    assert cfg["memory"]["backend"] == "builtin"
    assert cfg["cron"]["store"] == "/home/node/.openclaw/cron/jobs.json"
    assert cfg["hooks"]["maxBodyBytes"] == 1_048_576
    assert "allowCommands" in cfg["gateway"]["nodes"]
    assert "commands" not in cfg["gateway"]["nodes"]
    defaults = cfg["agents"]["defaults"]
    assert defaults["runRetries"]["max"] > 0
    assert defaults["memorySearch"]["enabled"] is True
    browser = cfg["browser"]
    assert browser["localLaunchTimeoutMs"] == 20_000
    assert browser["localCdpReadyTimeoutMs"] == 30_000
    assert browser["tabCleanup"] == {"idleMinutes": 30, "maxTabsPerSession": 4}


@pytest.mark.parametrize("emit_81", [True, False])
def test_node_command_allowlist_is_identical_across_shapes(monkeypatch, emit_81):
    """Only the KEY moved at 8.1 (gateway.nodes.allowCommands ->
    gateway.nodes.commands.allow). The list itself is a Stage-2 gate mirroring
    what the Rust desktop client advertises, so a difference between the two
    shapes would silently change what a paired Mac may be asked to run."""
    nodes = _cfg(monkeypatch, emit_81=emit_81)["gateway"]["nodes"]
    allow = nodes["commands"]["allow"] if emit_81 else nodes["allowCommands"]
    assert "screen.snapshot" in allow
    assert "system.run" in allow
    assert len(allow) == 16
