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

from core.containers.config import build_openclaw_config_dict
from core.services import config_schema


@pytest.fixture(autouse=True)
def _emit_81_config_shape():
    """Override the directory-wide fixture, which pins every config to the 8.1
    shape. This module's whole job is to exercise BOTH sides of the gate, and it
    does so the way production does -- by passing the tag of the image the
    config will run on -- so it must not have the gate forced open.
    """
    yield


_COMMON = {"user_id": "u_1", "gateway_token": "t" * 32, "provider_choice": "chatgpt_oauth"}

# Keys 2026.8.1 retired that a 7.2 container still honours.
_LEGACY_ROOT = ("audit", "web")


def _cfg(monkeypatch, *, emit_81: bool) -> dict:
    # Drive the gate the way production does -- by the tag of the image the
    # config will RUN on -- rather than by patching a version predicate. The
    # predicate is no longer on the path: reading this environment's pin
    # instead of the target image is the bug these tests now guard.
    tag = "2026.8.1-isol8.1-1c67107" if emit_81 else "2026.7.2-isol8.8-8f700f0"
    return build_openclaw_config_dict(**_COMMON, deployed_tag=tag)


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


# Keys 2026.8.1 retired inside the `acp` block. Sourced from upstream's own
# src/config/dead-config-keys.test.ts, not guessed.
_DEAD_ACP = ("coalesceIdleMs", "maxChunkChars", "maxOutputChars", "ttlMinutes", "maxConcurrentSessions")


def test_81_shape_drops_every_retired_acp_key(monkeypatch):
    """The acp block is only emitted when acp_enabled=True, which the other
    tests here do not pass -- so the schema test above walked straight past it
    and the retired acp keys shipped to prod.

    They are not cosmetic. 8.1's root schema is STRICT: one unrecognized key
    fails the whole file, doctor restores last-known-good, and the container
    silently keeps its PREVIOUS config. That is how the founder came up on 8.1
    still carrying the pre-narrowing wide gateway.trustedProxies and 403ing
    every gateway RPC -- a healthy-looking boot on a stale config.

    Asserted by key name rather than by schema validation on purpose: the
    vendored schema sets no additionalProperties on `acp`, so validation alone
    would not have caught this and would give false assurance.
    """
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107", acp_enabled=True)
    acp = cfg.get("acp")
    assert acp, "acp block missing -- this test is no longer exercising the path it guards"

    flat = {**acp, **acp.get("runtime", {}), **acp.get("stream", {})}
    for key in _DEAD_ACP:
        assert key not in flat, f"{key} is retired at 8.1 and fails the strict root schema"

    # And the whole config must still validate with the block present.
    assert config_schema.schema_errors(cfg) == []


def test_72_shape_still_emits_the_acp_tuning_a_72_container_honours(monkeypatch):
    """Mirror of the above: on 7.2 these keys are live and dropping them is a
    real behaviour change, not a cleanup."""
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.7.2-isol8.8-8f700f0", acp_enabled=True)
    acp = cfg["acp"]
    assert acp["runtime"] == {"ttlMinutes": 120}
    assert acp["stream"] == {"coalesceIdleMs": 300, "maxChunkChars": 1200}


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


# --- target-image derivation ------------------------------------------------
#
# Regression for a live founder-container outage. The rollback regen wrote
# 8.1-only keys AND meta.lastTouchedVersion=2026.8.1 onto a 7.2 image, because
# the shape gate and the meta stamp both read this ENVIRONMENT's pin (still 8.1
# mid-rollback) rather than the image the config would actually run on. The 7.2
# binary refused the stamp -- "older than the config last written by OpenClaw
# 2026.8.1" -- before it ever reached the keys, and the container crash-looped.


def _cfg_with_pin_ahead(monkeypatch, deployed_tag):
    """Pin deliberately AHEAD of the target, i.e. mid-rollback."""
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.8.1-isol8.1-1c67107")
    return build_openclaw_config_dict(**_COMMON, deployed_tag=deployed_tag)


def test_rollback_target_gets_72_shape_and_stamp_despite_81_pin(monkeypatch):
    cfg = _cfg_with_pin_ahead(monkeypatch, "2026.7.2-isol8.8-8f700f0")

    assert cfg["meta"]["lastTouchedVersion"] == "2026.7.2"
    assert "search" not in cfg.get("memory", {})
    assert "commands" not in cfg.get("gateway", {}).get("nodes", {})
    assert "mode" not in cfg.get("skills", {}).get("workshop", {}).get("autonomous", {})
    assert cfg["memory"]["backend"] == "builtin"
    assert "allowCommands" in cfg["gateway"]["nodes"]


def test_forward_target_gets_81_shape_and_stamp(monkeypatch):
    cfg = _cfg_with_pin_ahead(monkeypatch, "2026.8.1-isol8.1-1c67107")

    assert cfg["meta"]["lastTouchedVersion"] == "2026.8.1"
    assert "search" in cfg["memory"]
    assert "backend" not in cfg["memory"]


def test_absent_tag_falls_back_to_the_env_pin(monkeypatch):
    """A fresh provision passes no tag; the pin IS correct for that case."""
    cfg = _cfg_with_pin_ahead(monkeypatch, None)
    assert cfg["meta"]["lastTouchedVersion"] == "2026.8.1"


# --- gateway.trustedProxies -------------------------------------------------
#
# 2026.8.1 added ingress attribution: on the WebSocket upgrade, a peer INSIDE
# trustedProxies is treated as a proxy and must forward headers naming the real
# client. Our backend sends none -- it is the client, not a proxy -- so listing
# any range that contains its VPC address makes every upgrade 403
# ("unattributable-proxy", the only 403 on that path). The container boots
# perfectly and is simply unreachable from the backend.
#
# Asserted as "no network range at all" rather than "not 10.0.0.0/8" on purpose.
# The defect is not that one CIDR; it is any CIDR wide enough to contain the
# backend. A literal-string test would pass while someone added 172.16/12 for a
# future VPC peering and silently reintroduced the outage.

_RFC1918_PREFIXES = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
)


def test_trusted_proxies_is_loopback_only(monkeypatch):
    """trustedProxies must never contain a network range.

    Loopback is legitimate (the node-host connects over it). Anything wider
    catches the backend and 403s every gateway RPC on 2026.8.1+.
    """
    for tag in ("2026.7.2-isol8.8-8f700f0", "2026.8.1-isol8.1-1c67107"):
        cfg = build_openclaw_config_dict(**_COMMON, deployed_tag=tag)
        proxies = cfg["gateway"]["trustedProxies"]

        assert proxies == ["127.0.0.1", "::1"], (tag, proxies)
        # Belt and braces: no CIDR notation, no RFC1918 prefix, on either shape.
        for entry in proxies:
            assert "/" not in entry, (tag, entry)
            assert not entry.startswith(_RFC1918_PREFIXES), (tag, entry)
