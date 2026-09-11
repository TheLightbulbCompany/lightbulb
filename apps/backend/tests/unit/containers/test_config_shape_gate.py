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

import json

import pytest

from core import openclaw_version
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
    assert "mcp.tools.call.v1" in allow
    # 2026.8.1's node host / bundled browser plugin declare these unconditionally
    # (upstream node-host/runtime.ts + browser plugin-registration.ts); missing
    # any of them fails the gateway's `declared <= allowed` node-surface check
    # on every reconnect (see desktop_pair_live.approve_pending_node_surface).
    assert "fs.listDir" in allow
    assert "terminal.upload" in allow
    assert "browser.proxy.upload.v1" in allow
    assert len(allow) == 20


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


# --- agents roster shape ----------------------------------------------------
#
# 2026.8.1 moved the roster from ``agents.list`` (array, each entry carrying its
# own ``id``) to ``agents.entries`` (map keyed by agent id, ``id`` stripped from
# the value). Upstream's own load-time migration is the authoritative
# translation -- src/config/legacy.roster.ts at v2026.8.1-isol8.1:
#
#     const entries = Object.fromEntries(
#       legacyList.map((value) => { const { id, ...config } = value; return [id, config]; }),
#     );
#     const { list: _list, ...rest } = agents;
#     agents = { ...rest, entries };
#
# ``AgentsSchema`` is ``.strict()`` over {ownership, defaults, entries}, so at
# 8.1 ``list`` is not a config key at all -- it is re-attached AFTER validation
# as a NON-ENUMERABLE projection (src/config/agent-list-projection.ts), which is
# why it never survives serialization either. Emitting it writes to a key the
# gateway does not read: the roster goes to zero on every regenerate until the
# container is bounced and boot doctor re-migrates the file. Observed live on
# the founder container during the 8.1 rollout (isol8 #2036).

_FRIDAY = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_ATLAS = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
_ROSTER = [
    {"id": _FRIDAY, "name": "Friday", "role": "ceo"},
    {"id": _ATLAS, "name": "Atlas", "role": "general"},
]


def test_81_shape_emits_keyed_agents_entries_and_no_list():
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107", paperclip_agents=_ROSTER)
    agents = cfg["agents"]

    assert "list" not in agents, "agents.list is a dead key at 8.1 -- the roster reads entries"
    entries = agents["entries"]
    assert set(entries) == {_FRIDAY, _ATLAS}
    # The id is the KEY. The entry value schema is additionalProperties:false
    # (upstream: AgentEntrySchema.omit({ id: true })), so an ``id`` left inside
    # the value fails the whole file.
    assert all("id" not in entry for entry in entries.values()), entries
    assert entries[_FRIDAY]["name"] == "Friday"
    assert entries[_FRIDAY]["workspace"] == f"/home/node/.openclaw/workspace-{_FRIDAY}"
    # Exactly one legacy default=true marker. 8.1 still accepts it explicitly
    # ("one legacy default=true marker" -- zod-schema.agents.ts) and boot doctor
    # consumes it to materialize per-surface ownership. A multi-agent roster with
    # neither a marker nor agents.ownership="explicit" is REFUSED outright.
    assert [k for k, v in entries.items() if v.get("default") is True] == [_FRIDAY]
    assert config_schema.schema_errors(cfg) == []


def test_81_shape_keys_the_unprovisioned_fallback_agent_too():
    """No Paperclip roster yet -> the legacy single 'main' entry, still keyed."""
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107")
    agents = cfg["agents"]

    assert "list" not in agents
    assert list(agents["entries"]) == ["main"]
    assert "id" not in agents["entries"]["main"]
    assert config_schema.schema_errors(cfg) == []


def test_81_shape_names_the_system_agent_owner_explicitly():
    """The marker alone is not enough on a RUNNING 8.1 container: boot doctor
    turns it into agents.defaults.systemAgent.agentId, but a config regenerate
    without a restart does not, and a multi-agent roster with no explicit owner
    fails the gateway's own health RPC (AgentSelectionRequiredError). The
    generator names the owner itself, and it is the same agent the marker is on."""
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107", paperclip_agents=_ROSTER)
    assert cfg["agents"]["defaults"]["systemAgent"] == {"agentId": _FRIDAY}
    assert config_schema.schema_errors(cfg) == []

    # No CEO -> the first agent owns ambient work, marker and owner agree.
    no_ceo = [{"id": _ATLAS, "name": "Atlas", "role": "general"}, {"id": _FRIDAY, "name": "Friday", "role": "general"}]
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107", paperclip_agents=no_ceo)
    assert cfg["agents"]["defaults"]["systemAgent"] == {"agentId": _ATLAS}
    assert cfg["agents"]["entries"][_ATLAS]["default"] is True

    # Unprovisioned fallback: the legacy single entry owns itself.
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107")
    assert cfg["agents"]["defaults"]["systemAgent"] == {"agentId": "main"}


def test_81_roster_lists_the_ceo_first():
    """After the loader strips the marker, agents.list reports the FIRST entry
    as defaultId (the explicit-ownership fallback). The founder's roster listed
    Tally before Friday, and every "Ask Friday" exit opened Tally. Order is the
    only signal that resolver reads, so the CEO goes first; the rest keep
    Paperclip's order."""
    tally = "11111111-1111-1111-1111-111111111111"
    pulse = "22222222-2222-2222-2222-222222222222"
    roster = [
        {"id": tally, "name": "Tally", "role": "general"},
        {"id": pulse, "name": "Pulse", "role": "general"},
        {"id": _FRIDAY, "name": "Friday", "role": "ceo"},
    ]
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.8.1-isol8.1-1c67107", paperclip_agents=roster)
    assert list(cfg["agents"]["entries"]) == [_FRIDAY, tally, pulse]
    # json.dumps preserves dict order, so the file the gateway reads does too.
    assert list(json.loads(json.dumps(cfg))["agents"]["entries"]) == [_FRIDAY, tally, pulse]


def test_72_shape_has_no_system_agent_key():
    """7.2's agents.defaults is .strict(); the key would fail the whole file."""
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.7.2-isol8.8-8f700f0", paperclip_agents=_ROSTER)
    assert "systemAgent" not in cfg["agents"]["defaults"]


def test_72_shape_still_emits_the_agents_list_array():
    """Mirror: on 7.2 ``entries`` does not exist and ``list`` is the roster."""
    cfg = build_openclaw_config_dict(**_COMMON, deployed_tag="2026.7.2-isol8.8-8f700f0", paperclip_agents=_ROSTER)
    agents = cfg["agents"]

    assert "entries" not in agents, "entries is not a 7.2 key -- its strict schema refuses it"
    ids = [entry["id"] for entry in agents["list"]]
    assert ids == [_FRIDAY, _ATLAS]
    assert agents["list"][0]["default"] is True


def test_model_policy_is_81_only(monkeypatch):
    """``agents.defaults.modelPolicy`` does not exist in the 7.2 schema and
    ``agents.defaults`` is ``.strict()`` there, so emitting it onto a 7.2 image
    fails config validation and the gateway refuses to start. On 8.1 it is what
    makes the picker resolve against the account-scoped catalog (#115953)."""
    assert _cfg(monkeypatch, emit_81=True)["agents"]["defaults"]["modelPolicy"] == {"allow": ["openai/*"]}
    assert "modelPolicy" not in _cfg(monkeypatch, emit_81=False)["agents"]["defaults"]


def test_model_selection_scope_is_81_only(monkeypatch):
    """``agents.defaults.modelSelectionScope`` is an 8.1 key, and 7.2's
    ``agents.defaults`` is ``.strict()``. "session" is what stops a chat
    ``/model`` directive persisting the picked model back into the file the
    backend owns (sticky-model-selection.ts defaults an unset value to
    "effective", which writes)."""
    assert _cfg(monkeypatch, emit_81=True)["agents"]["defaults"]["modelSelectionScope"] == "session"
    assert "modelSelectionScope" not in _cfg(monkeypatch, emit_81=False)["agents"]["defaults"]


def _mcp_cfg(tag: str) -> dict:
    """A config carrying an mcp block — runtimeScope is only written when one exists."""
    return build_openclaw_config_dict(
        **_COMMON,
        deployed_tag=tag,
        integration_mcp_servers={"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"]}},
    )


def test_mcp_runtime_scope_follows_the_target_image_not_the_env_pin(monkeypatch):
    """``mcp.runtimeScope`` exists only from 2026.6.1; an older gateway's MCP
    schema is strict and crash-loops on the unknown key at boot.

    The gate used to read ``deployed_upstream_at_least`` (THIS environment's
    pin) while every sibling gate read ``deployed_tag``. Inert while the pin and
    the running image agree, but it is the one gate that could write a
    newer-only key onto a lagging container — an image roll BACK, or a
    reprovision mid-promotion — which is the exact failure ``_target_upstream``
    exists to prevent. Patch the old predicate open to prove it is off the path.
    """
    monkeypatch.setattr(openclaw_version, "deployed_upstream_at_least", lambda target: True)
    assert "runtimeScope" not in _mcp_cfg("2026.5.12-0654e00")["mcp"]
    assert _mcp_cfg("2026.8.1-isol8.3-abc1234")["mcp"]["runtimeScope"] == "shared"
