"""Tests for OpenClaw container config generation."""

import json
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from core.containers.config import (
    build_openclaw_config_dict,
    write_openclaw_config,
)
from core.services.agent_roster import roster_entries
from core.services.efs_file_lock import (
    OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL,
    OPENCLAW_CONFIG_PROTOCOL_SUFFIX,
    SidecarLockTimeoutError,
    openclaw_config_sidecar_lock,
)


_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def _cfg(provider_choice: str = "bedrock_claude", **kwargs) -> dict:
    """Build a config dict with sensible defaults for the per-section tests."""
    defaults = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
        "aip_arns": _AIPS,
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


def _version_parses_as_semver(value: str) -> bool:
    """Upstream compareOpenClawVersions needs dotted numerics; an arbitrary
    string trips warnIfConfigFromFuture instead of comparing cleanly."""
    parts = value.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)


class TestOpenclawConfigShape:
    """Verifies the static sections OpenClaw refuses to start without.

    Reducing ``write_openclaw_config`` to a stub regresses gateway / memory /
    tools / hooks / channels / browser — these must always be present
    regardless of provider_choice.
    """

    def test_gateway_token_auth(self):
        config = _cfg(gateway_token="test-token-123")
        assert config["gateway"]["mode"] == "local"
        assert config["gateway"]["bind"] == "lan"
        assert config["gateway"]["auth"] == {"mode": "token", "token": "test-token-123"}
        # Loopback ONLY. The VPC /8 that used to be here made 2026.8.1 treat the
        # backend itself as a proxy and 403 every WS upgrade; see
        # test_trusted_proxies_is_loopback_only for the invariant.
        assert config["gateway"]["trustedProxies"] == ["127.0.0.1", "::1"]

    def test_control_ui_disabled(self):
        config = _cfg()
        assert config["gateway"]["controlUi"]["enabled"] is False

    def test_chat_completions_disabled(self):
        """We use WebSocket RPC, not Bedrock-compatible chat completions."""
        config = _cfg()
        assert config["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"] is False

    def test_audit_ledger_not_emitted(self):
        """OpenClaw 2026.8.1 removed the root ``audit`` key (the ledger moved
        under ``logging.audit``) and the root schema is strict, so emitting it
        is a boot-time validation failure. We only ever set enabled:False --
        which is the default -- so the key is simply dropped rather than
        relocated. The original reason still holds: the ledger's per-event
        transactions were the dominant SQLite lock contender on EFS and nothing
        in Isol8 consumes audit.* RPCs."""
        config = _cfg()
        assert "audit" not in config
        assert "audit" not in config.get("logging", {})

    def test_agents_defaults_workspace_routes_to_efs(self):
        config = _cfg()
        assert config["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspace"

    def test_meta_shim_uses_real_upstream_version(self):
        """`meta.lastTouchedVersion` must be the real openclaw version
        (parses as semver in upstream's compareOpenClawVersions); arbitrary
        strings work for hasConfigMeta but trigger spurious "from future"
        warnings in warnIfConfigFromFuture (io.ts).

        It tracks the TARGET image rather than this environment's pin. Stamping
        the pin took the founder container down: a rollback to 7.2 wrote
        lastTouchedVersion 2026.8.1 (the pin, mid-revert), and the 7.2 binary
        refused to start -- "older than the config last written by OpenClaw
        2026.8.1" -- before it ever parsed a key. The fallback-to-pin path a
        fresh provision uses is covered in test_config_shape_gate.py."""
        config = _cfg()
        assert config["meta"]["lastTouchedVersion"] == "2026.8.1"  # forced by the directory fixture
        assert _version_parses_as_semver(config["meta"]["lastTouchedVersion"])

    def test_agents_defaults_memory_search_enabled(self):
        # memorySearch is ON for every owner (memory-core registers
        # memory_search/memory_get, and the "Memory Recall" prompt section only
        # renders while those tools exist). _cfg() defaults to bedrock_claude,
        # which resolves to FTS keyword recall (provider:"none", no model) —
        # semantic embeddings would run Bedrock Titan on our AWS account, and we
        # never pay for embeddings. Only byo_key+openai gets a vector provider
        # (see test_config_provider_routing).
        config = _cfg()
        # 2026.8.1 moved agents.defaults.memorySearch to the root memory.search.
        assert "memorySearch" not in config["agents"]["defaults"]
        memory_search = config["memory"]["search"]
        assert memory_search["enabled"] is True
        assert memory_search["provider"] == "none"
        assert "model" not in memory_search

    def test_agents_defaults_no_llm_key(self):
        # `agents.defaults.llm` was removed from AgentDefaultsSchema between
        # OpenClaw v2026.4.23 and v2026.5.7. The strict() schema rejects it
        # with `Unrecognized key: "llm"` and the gateway exits at startup.
        # OpenClaw's default idle timeout is fine.
        config = _cfg()
        assert "llm" not in config["agents"]["defaults"]

    def test_agents_defaults_verbose_full(self):
        """verboseDefault=full keeps tool result/partialResult in agent
        events so the frontend can show tool input + output."""
        config = _cfg()
        assert config["agents"]["defaults"]["verboseDefault"] == "full"

    def test_main_agent_has_absolute_workspace(self):
        """Path.resolve() returns absolute paths unchanged regardless of
        process cwd. Relative breaks because exec tools run with cwd=workspaceDir."""
        config = _cfg()
        main_entry = next(a for a in roster_entries(config) if a.get("id") == "main")
        assert main_entry["workspace"] == "/home/node/.openclaw/workspace-main"
        assert main_entry["default"] is True
        assert main_entry["reasoningDefault"] == "stream"

    def test_memory_builtin_backend(self):
        """Do NOT switch to the qmd backend (EFS NFS deadlock class).
        Builtin = flat MEMORY.md files in agent workspace, no sqlite."""
        config = _cfg()
        memory = config["memory"]
        # 2026.8.1 retired the qmd backend entirely, so ``memory.backend`` is
        # gone from the schema and builtin is the only shape there is. Dropping
        # the key is what keeps the strict parse passing; the hazard it guarded
        # against (qmd on EFS) can no longer be selected at all.
        assert "backend" not in memory
        assert memory["citations"] == "auto"
        # qmd block must be entirely absent — leftover keys would resurrect
        # the old codepath if openclaw ever tolerates them.
        assert "qmd" not in memory

    def test_tools_full_profile_denies_canvas_nodes(self):
        """`nodes` is toggled to enabled at runtime by node_proxy.py when a
        desktop node pairs; default-deny prevents exposure without one."""
        config = _cfg()
        assert config["tools"]["profile"] == "full"
        assert "canvas" in config["tools"]["deny"]
        assert "nodes" in config["tools"]["deny"]

    def test_tools_explicit_wildcard_allow_present(self):
        """An explicit ``tools.allow: ["*"]`` is required (separate from
        ``profile: "full"``) so the optional-plugin-tool gate
        (OpenClaw isOptionalToolAllowed) surfaces optional:true tools like
        lobster / llm-task. profile-full's implicit wildcard does NOT reach
        that gate — it resolves empty there and hides optional tools. The
        explicit wildcard must coexist with the canvas/nodes denials, which
        still win over it."""
        config = _cfg()
        assert config["tools"]["allow"] == ["*"]
        # deny must still be intact — wildcard allow does not override deny.
        assert "canvas" in config["tools"]["deny"]
        assert "nodes" in config["tools"]["deny"]

    def test_workflow_plugins_enabled(self):
        """lobster + llm-task + aws-exec-profile + tokenjuice + sage-openclaw
        are bundled, provider-independent OpenClaw extensions enabled on every
        provision. llm-task is routed to the cheap tier where one exists
        (shape pinned per-path in test_config_cost_tuning) — its configSchema
        explicitly allows defaultProvider/defaultModel. aws-exec-profile
        injects the owner's read-only AWS profile into the agent's exec env
        only (inert until that profile is materialized). tokenjuice and
        sage-openclaw both have strict-EMPTY configSchemas, so their entries
        must stay enable-only — a config key bricks the container at boot."""
        config = _cfg()
        entries = config["plugins"]["entries"]
        assert entries["lobster"] == {"enabled": True}
        assert entries["llm-task"]["enabled"] is True
        assert entries["aws-exec-profile"] == {"enabled": True}
        assert entries["tokenjuice"] == {"enabled": True}
        assert entries["sage-openclaw"] == {"enabled": True}

    def test_memory_core_dreaming_enabled_light_and_rem(self):
        """memory-core's dreaming subsystem is enabled at provision with the
        light and REM phases ON and deep OFF. Light-phase consolidates recent
        session transcripts into dated memory/<day>.md notes that auto-load —
        cross-session continuity without relying on discretionary writes. REM
        writes DREAMS.md narratives, now surfaced by the Dreams control panel's
        Diary tab. Deep stays OFF by default — it is an opt-in per-container
        toggle, not a builder flip. Only schema-legal keys may appear under
        plugins.entries.memory-core.config — the upstream configSchema is
        additionalProperties:false at every level, so a stray key bricks the
        container at boot."""
        config = _cfg()
        entry = config["plugins"]["entries"]["memory-core"]
        assert entry["enabled"] is True
        dreaming = entry["config"]["dreaming"]
        assert dreaming["enabled"] is True
        assert dreaming["frequency"] == "0 3 * * *"
        assert dreaming["timezone"] == "UTC"
        assert dreaming["phases"]["light"] == {"enabled": True}
        assert dreaming["phases"]["rem"] == {"enabled": True}
        assert dreaming["phases"]["deep"] == {"enabled": False}
        assert set(entry["config"].keys()) == {"dreaming"}

    def test_memory_wiki_shared_company_vault(self):
        """memory-wiki (bundled in the base image) is enabled at provision as
        the owner's company brain: ONE vault shared by every agent in the
        container. vault.scope stays "global" explicitly — flipping it to
        "agent" would silently fragment the shared vault per-agent, which
        defeats the feature; renderMode "obsidian" writes [[wikilink]]
        markdown. Only schema-legal keys may appear (the upstream configSchema
        is additionalProperties:false at every level; vault accepts only
        scope/path/renderMode) — a stray key bricks the container at boot.

        Leaving `vault.path` unset is also load-bearing OUTSIDE the container:
        the plugin then resolves the vault to its `~/.openclaw/wiki/main`
        default, which is what routers/brain.py's BRAIN_SUBTREE roots the
        owner's Brain panel at. Setting a path here without moving that
        constant points the panel at an empty directory."""
        config = _cfg()
        entry = config["plugins"]["entries"]["memory-wiki"]
        assert entry["enabled"] is True
        assert entry["config"]["vault"] == {"scope": "global", "renderMode": "obsidian"}
        assert set(entry["config"].keys()) == {"vault"}

    def test_tools_exec_approval_policy(self):
        """mode="full" runs exec without an approval gate. Agents run
        autonomously via the Paperclip openclaw_gateway adapter with no
        operator to answer an approval; the old allowlist/on-miss policy
        made every autonomous exec hang on an approval that never arrived
        (120s gateway timeout). The per-user container is an isolated
        sandbox; this mirrors Paperclip's dangerouslySkipPermissions
        default for unattended agents. `mode` is mutually exclusive with
        security/ask. OpenClaw's default security=deny would silently
        block every exec call (exec-defaults.ts)."""
        config = _cfg()
        exec_cfg = config["tools"]["exec"]
        assert exec_cfg["mode"] == "full"
        assert "security" not in exec_cfg
        assert "ask" not in exec_cfg

    def test_tools_image_understanding_enabled(self):
        config = _cfg()
        assert config["tools"]["media"]["image"]["enabled"] is True

    def test_tools_web_fetch_enabled(self):
        config = _cfg()
        assert config["tools"]["web"]["fetch"]["enabled"] is True

    def test_skills_node_manager(self):
        config = _cfg()
        assert config["skills"]["install"]["nodeManager"] == "npm"

    def test_hooks_internal_entries(self):
        config = _cfg()
        entries = config["hooks"]["internal"]["entries"]
        assert entries["command-logger"]["enabled"] is True
        assert entries["session-memory"]["enabled"] is True

    def test_channels_empty_until_configured(self):
        """Channels ship EMPTY at provision — a provider block only appears once
        the wizard PATCHes the first account. Shipping the enabled-trio made every
        un-set-up owner count 3 'configured' channels, which broke agent-authored
        delivery crons (delivery.channel ambiguity) and left an orphan-credential
        crash-loop substrate."""
        config = _cfg()
        assert config["channels"] == {}

    def test_session_dm_scope(self):
        config = _cfg()
        assert config["session"]["dmScope"] == "per-account-channel-peer"

    def test_session_maintenance_enforce_with_bounds(self):
        """Enforce mode with raised bounds (full shape + rationale pinned in
        test_config_tier1b). The old "warn" posture never deleted anything —
        unbounded session-store growth on EFS; enforce preserves durable human
        conversations and reaps only synthetic rows."""
        config = _cfg()
        assert config["session"]["maintenance"]["mode"] == "enforce"
        assert config["session"]["maintenance"]["maxEntries"] == 2000

    def test_browser_two_profiles_default_openclaw(self):
        """Both profiles are statically defined; default is the cloud
        Chromium profile so unbound sessions (channels, cron, unpaired
        UI) don't try to hit a non-existent desktop. The `user` profile
        only routes when a desktop node is paired."""
        config = _cfg()
        browser = config["browser"]
        assert browser["enabled"] is True
        assert browser["defaultProfile"] == "openclaw"
        # Container Chromium (bundled in the extended image).
        assert browser["profiles"]["openclaw"]["driver"] == "openclaw"
        # 2026.8.1 removed browser.profiles.*.color from the strict profile
        # object (the built-in colour applies); emitting it fails the parse.
        assert "color" not in browser["profiles"]["openclaw"]
        assert "color" not in browser["profiles"]["user"]
        # cdpPort required by upstream schema since v2026.5.7-0d10b38:
        # zod-schema.ts refines any non-`existing-session` profile
        # to require cdpPort or cdpUrl. If this assertion ever fails
        # the container will boot-loop with "Profile must set cdpPort
        # or cdpUrl".
        assert isinstance(browser["profiles"]["openclaw"]["cdpPort"], int)
        assert 1 <= browser["profiles"]["openclaw"]["cdpPort"] <= 65535
        # Real signed-in Chrome via Chrome DevTools MCP — only routable
        # when the desktop sidecar is paired as a browser-capable node.
        # `existing-session` is the one driver the schema exempts from
        # the cdpPort-or-cdpUrl check, so this profile correctly omits both.
        assert browser["profiles"]["user"]["driver"] == "existing-session"
        assert "cdpPort" not in browser["profiles"]["user"]
        assert "cdpUrl" not in browser["profiles"]["user"]

    def test_no_node_host_block(self):
        """`nodeHost.browserProxy` is a node-host-side config (read by
        `openclaw node run` on the desktop), not a gateway config. It
        must not appear in the gateway/container's openclaw.json — it
        was dead config there."""
        config = _cfg()
        assert "nodeHost" not in config

    def test_gateway_nodes_browser_mode_personal_auto(self):
        """Personal owners: one human, one Mac — auto-route browser tool
        calls to the paired desktop when one's connected."""
        config = _cfg(owner_type="personal")
        assert config["gateway"]["nodes"]["browser"]["mode"] == "auto"

    def test_gateway_nodes_browser_mode_org_manual(self):
        """Org owners: multi-member ambiguity — agent must explicitly
        pick a node (`target: "node"` + `node: <id>`) per call,
        otherwise selectDefaultNodeFromList could route a peer's
        request to the wrong member's Chrome."""
        config = _cfg(owner_type="org")
        assert config["gateway"]["nodes"]["browser"]["mode"] == "manual"

    def test_gateway_nodes_browser_mode_defaults_to_personal(self):
        """Backwards-compat: callers that don't pass owner_type get
        the personal default."""
        config = _cfg()
        assert config["gateway"]["nodes"]["browser"]["mode"] == "auto"

    def test_update_check_disabled(self):
        config = _cfg()
        assert config["update"]["checkOnStart"] is False

    def test_owner_chat_commands_enabled(self):
        """The chat ``commands`` flag block is curated for the slash-command
        menu. The control panel owns MCP/plugins/config writes, so /mcp,
        /plugins, and /config must not be mutable from chat. /bash (host-shell)
        and /restart (gateway lifecycle) widen the trust boundary too far for
        chat. /debug stays on — runtime-only diagnostics that don't persist."""
        config = _cfg()
        assert config["commands"]["debug"] is True
        assert config["commands"]["restart"] is False
        assert "mcp" not in config["commands"], "/mcp must stay off — the control panel owns MCP server writes"
        assert "plugins" not in config["commands"], "/plugins must stay off — the control panel owns plugin enablement"
        assert "config" not in config["commands"], (
            "/config must stay off — chat must not be able to mutate provider/billing keys"
        )
        assert "bash" not in config["commands"], "/bash must stay off — host-shell access from chat is too broad"

    def test_tools_elevated_not_enabled(self):
        """tools.elevated.enabled gates the elevated tool path. We don't
        need it because /bash (its only consumer here) is intentionally
        disabled. If a future PR re-enables /bash, it must also flip
        tools.elevated.enabled — the schema requires both."""
        config = _cfg()
        elevated = config.get("tools", {}).get("elevated") or {}
        assert elevated.get("enabled") is not True

    def test_no_root_tts_key(self):
        """OpenClaw doesn't support tts at root level."""
        config = _cfg()
        assert "tts" not in config

    def test_full_config_round_trips_through_json(self):
        """Whole config must serialize cleanly — no non-JSON values like
        sets, datetime, or unresolved coroutines slipped in."""
        config = _cfg()
        round_tripped = json.loads(json.dumps(config))
        assert round_tripped["gateway"]["mode"] == "local"


class TestOpenclawConfigStaticSectionsAcrossProviders:
    """Static sections must be identical regardless of provider_choice."""

    @pytest.mark.parametrize(
        "provider_choice,kwargs",
        [
            ("chatgpt_oauth", {}),
            ("byo_key", {"byo_provider": "openai"}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
        ],
    )
    def test_gateway_present_for_every_provider(self, provider_choice, kwargs):
        config = _cfg(provider_choice=provider_choice, **kwargs)
        assert "mode" in config["gateway"]
        assert config["gateway"]["mode"] == "local"

    @pytest.mark.parametrize(
        "provider_choice,kwargs",
        [
            ("chatgpt_oauth", {}),
            ("byo_key", {"byo_provider": "openai"}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
        ],
    )
    def test_channels_empty_for_every_provider(self, provider_choice, kwargs):
        config = _cfg(provider_choice=provider_choice, **kwargs)
        assert config["channels"] == {}

    @pytest.mark.parametrize(
        "provider_choice,kwargs",
        [
            ("chatgpt_oauth", {}),
            ("byo_key", {"byo_provider": "openai"}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
        ],
    )
    def test_workflow_plugins_enabled_for_every_provider(self, provider_choice, kwargs):
        """The static lobster/llm-task entries must merge alongside the
        provider-derived plugin entries (e.g. bedrock's ``amazon-bedrock``)
        rather than clobbering them — and vice versa."""
        config = _cfg(provider_choice=provider_choice, **kwargs)
        entries = config["plugins"]["entries"]
        assert entries["lobster"] == {"enabled": True}
        # llm-task's per-path cheap-tier config is pinned in
        # test_config_cost_tuning; here only that the entry survives the merge.
        assert entries["llm-task"]["enabled"] is True
        assert entries["aws-exec-profile"] == {"enabled": True}
        assert entries["tokenjuice"] == {"enabled": True}
        assert entries["sage-openclaw"] == {"enabled": True}
        # The provider block's own entry must survive the merge.
        if provider_choice in ("bedrock_claude", "bedrock_trial"):
            assert entries["amazon-bedrock"]["config"]["discovery"]["enabled"] is False


@pytest.mark.asyncio
class TestWriteOpenclawConfigAsync:
    """Async write wrapper writes valid JSON to disk."""

    async def test_writes_full_config_to_disk(self, tmp_path):
        out = tmp_path / "openclaw.json"
        await write_openclaw_config(
            config_path=out,
            gateway_token="t-1",
            provider_choice="bedrock_claude",
            user_id="u_1",
            aip_arns=_AIPS,
        )
        cfg = json.loads(out.read_text())
        # Sanity-check one field from each major section.
        assert cfg["gateway"]["auth"]["token"] == "t-1"
        assert cfg["agents"]["defaults"]["verboseDefault"] == "full"
        assert cfg["tools"]["exec"]["mode"] == "full"
        assert cfg["channels"] == {}
        assert cfg["browser"]["defaultProfile"] == "openclaw"
        assert "nodeHost" not in cfg
        assert cfg["gateway"]["nodes"]["browser"]["mode"] == "auto"
        # Phase one remains on the legacy persistent sidecar until boot
        # recovery publishes the protocol marker.
        assert (tmp_path / "openclaw.json.lock").is_file()
        assert (tmp_path / "openclaw.json.lock.isol8-guard").is_file()

    async def test_post_transition_full_write_uses_ephemeral_sidecar(self, tmp_path):
        out = tmp_path / "openclaw.json"
        lock_path = tmp_path / "openclaw.json.lock"
        (tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_PROTOCOL_SUFFIX}").write_bytes(
            OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL
        )

        await write_openclaw_config(
            config_path=out,
            gateway_token="t-ephemeral",
            provider_choice="bedrock_claude",
            user_id="u_1",
            aip_arns=_AIPS,
        )

        assert json.loads(out.read_text())["gateway"]["auth"]["token"] == "t-ephemeral"
        assert not lock_path.exists()

    async def test_post_transition_full_write_waits_for_openclaw_sidecar(self, tmp_path):
        out = tmp_path / "openclaw.json"
        out.write_text('{"sentinel":"unchanged"}')
        lock_path = tmp_path / "openclaw.json.lock"
        lock_path.write_text('{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}')
        (tmp_path / f"openclaw.json.lock{OPENCLAW_CONFIG_PROTOCOL_SUFFIX}").write_bytes(
            OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL
        )

        with (
            patch(
                "core.containers.config.openclaw_config_sidecar_lock",
                partial(openclaw_config_sidecar_lock, timeout_s=0),
            ),
            pytest.raises(SidecarLockTimeoutError, match="sidecar"),
        ):
            await write_openclaw_config(
                config_path=out,
                gateway_token="must-not-land",
                provider_choice="bedrock_claude",
                user_id="u_1",
                aip_arns=_AIPS,
            )

        assert json.loads(out.read_text()) == {"sentinel": "unchanged"}
        assert lock_path.read_text() == '{"pid":123,"createdAt":"2026-08-15T00:00:00Z"}'

    async def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "openclaw.json"
        await write_openclaw_config(
            config_path=nested,
            gateway_token="t",
            provider_choice="bedrock_claude",
            user_id="u_1",
            aip_arns=_AIPS,
        )
        assert nested.exists()


# Deterministic Ed25519 public key in PEM form — same fixture used in
# test_desktop_pair.py. The derived deviceId is stable across runs.
_FIXTURE_PUBKEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAO2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik=
-----END PUBLIC KEY-----
"""
_FIXTURE_EXPECTED_DEVICE_ID = "139e3940e64b5491722088d9a0d741628fc826e09475d341a780acde3c4b8070"
# The 32-byte raw Ed25519 key from the PEM above, encoded as
# base64url-no-pad — what upstream's gateway compares paired.json against
# (publicKeyRawBase64UrlFromPem in openclaw/src/infra/device-identity.ts).
_FIXTURE_EXPECTED_PUBLIC_KEY_B64URL = "O2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik"


@pytest.mark.asyncio
class TestEnsureNodePairedEntry:
    """``ensure_node_paired_entry`` now takes a pubkey PEM directly — the
    backend no longer holds device private keys. Keys are owned by the
    client (macOS Keychain); backend just writes paired.json from a
    caller-supplied pubkey.
    """

    async def test_writes_entry_from_pubkey(self):
        from core.containers.config import ensure_node_paired_entry

        # ``WorkspaceLike`` now exposes a single ``mutate_paired_json`` that
        # does locked RMW. We capture the post-mutation state to assert on
        # the persisted shape.
        captured = {"state": None}

        async def fake_mutate(mutator):
            current = {}
            changed = mutator(current)
            if changed:
                captured["state"] = current

        mock_workspace = MagicMock()
        mock_workspace.mutate_paired_json = AsyncMock(side_effect=fake_mutate)

        added = await ensure_node_paired_entry(
            owner_id="owner_42",
            public_key_pem=_FIXTURE_PUBKEY_PEM,
            client_id="node-host",
            display_name="Alice | Isol8 Desktop (CLI)",
            commands=["system.run", "browser.proxy"],
            workspace=mock_workspace,
        )

        assert added is True
        mock_workspace.mutate_paired_json.assert_awaited_once()
        written = captured["state"]
        # REGRESSION: flat record (no `pairedByDeviceId` wrapper) — upstream
        # writes the inner record to disk, so the wrapper would make our
        # entry invisible to the gateway's load path.
        assert "pairedByDeviceId" not in written
        assert _FIXTURE_EXPECTED_DEVICE_ID in written
        entry = written[_FIXTURE_EXPECTED_DEVICE_ID]
        # Stored publicKey must be the base64url-no-pad raw key (the wire
        # format), NOT the PEM. Upstream's gateway does strict string
        # equality of paired.json's publicKey against the wire-format
        # pubkey in the signed connect frame (message-handler.ts:
        # 1151-1153).
        assert entry["publicKey"] == _FIXTURE_EXPECTED_PUBLIC_KEY_B64URL
        assert entry["clientId"] == "node-host"
        assert entry["role"] == "node"
        assert entry["commands"] == ["system.run", "browser.proxy"]
        assert entry["displayName"] == "Alice | Isol8 Desktop (CLI)"
        # deviceId derives from pubkey (sha256 of raw key bytes).
        assert entry["deviceId"] == _FIXTURE_EXPECTED_DEVICE_ID
        # tokens.node is load-bearing for the role-upgrade gate.
        assert "tokens" in entry and "node" in entry["tokens"]
        assert entry["tokens"]["node"]["token"]

    async def test_idempotent_when_pubkey_matches(self):
        """Calling twice with the same pubkey is a no-op on the second call."""
        from core.containers.config import ensure_node_paired_entry

        # Shared in-memory paired.json state across two mutate calls.
        state: dict = {}
        write_count = {"n": 0}

        async def fake_mutate(mutator):
            changed = mutator(state)
            if changed:
                write_count["n"] += 1

        mock_workspace = MagicMock()
        mock_workspace.mutate_paired_json = AsyncMock(side_effect=fake_mutate)

        added1 = await ensure_node_paired_entry(
            owner_id="o",
            public_key_pem=_FIXTURE_PUBKEY_PEM,
            client_id="node-host",
            display_name="N",
            commands=["system.run"],
            workspace=mock_workspace,
        )
        added2 = await ensure_node_paired_entry(
            owner_id="o",
            public_key_pem=_FIXTURE_PUBKEY_PEM,
            client_id="node-host",
            display_name="N",
            commands=["system.run"],
            workspace=mock_workspace,
        )

        assert added1 is True
        assert added2 is False
        assert write_count["n"] == 1

    async def test_preserves_other_paired_entries(self):
        """Adding a new device doesn't clobber existing paired entries."""
        from core.containers.config import ensure_node_paired_entry

        # Flat record (no wrapper) seeded with a prior Mac entry.
        state = {
            "deadbeef": {
                "deviceId": "deadbeef",
                "publicKey": "other-pem",
                "role": "node",
                "clientId": "openclaw-macos",
                "displayName": "Bob | Isol8 Mac",
                "commands": ["system.notify"],
                "tokens": {"node": {"token": "keep-me", "createdAtMs": 1}},
                "createdAtMs": 1,
                "approvedAtMs": 1,
            }
        }

        async def fake_mutate(mutator):
            mutator(state)

        mock_workspace = MagicMock()
        mock_workspace.mutate_paired_json = AsyncMock(side_effect=fake_mutate)

        await ensure_node_paired_entry(
            owner_id="o",
            public_key_pem=_FIXTURE_PUBKEY_PEM,
            client_id="node-host",
            display_name="Alice | CLI",
            commands=["system.run"],
            workspace=mock_workspace,
        )

        assert "deadbeef" in state
        assert state["deadbeef"]["tokens"]["node"]["token"] == "keep-me"
        assert _FIXTURE_EXPECTED_DEVICE_ID in state


class TestChannelAccountsReinjection:
    """Channel bot accounts + their route bindings must survive a reprovision.

    A reprovision fully overwrites openclaw.json; build_openclaw_config_dict
    merges carried-forward channel accounts back into the channels block and
    recreates the sibling top-level route bindings (config_patcher adds those
    per-PATCH, so they are also lost by the overwrite).
    """

    def test_channel_accounts_merged_into_channels_block(self):
        accounts = {"telegram": {"agent_a": {"botToken": "123:ABC", "dmPolicy": "pairing"}}}
        cfg = _cfg(channel_accounts=accounts)
        assert cfg["channels"]["telegram"]["accounts"] == {"agent_a": {"botToken": "123:ABC", "dmPolicy": "pairing"}}
        # The re-injected channel block is (re)created enabled so the plugin loads.
        assert cfg["channels"]["telegram"]["enabled"] is True
        # Providers without carried-forward accounts stay absent (empty-until-configured).
        assert "discord" not in cfg["channels"]

    def test_channel_accounts_recreate_route_bindings(self):
        accounts = {
            "telegram": {"agent_a": {"botToken": "123:ABC"}},
            "slack": {"agent_b": {"mode": "socket", "appToken": "xapp-", "botToken": "xoxb-"}},
        }
        cfg = _cfg(channel_accounts=accounts)
        assert {
            "type": "route",
            "agentId": "agent_a",
            "match": {"channel": "telegram", "accountId": "agent_a"},
        } in cfg["bindings"]
        assert {
            "type": "route",
            "agentId": "agent_b",
            "match": {"channel": "slack", "accountId": "agent_b"},
        } in cfg["bindings"]

    def test_no_channel_accounts_emits_empty_channels(self):
        cfg = _cfg()
        assert cfg["channels"] == {}
        assert cfg.get("bindings", []) == []

    def test_unknown_provider_in_channel_accounts_is_skipped(self):
        # A provider not in the static channels block must not crash or inject.
        cfg = _cfg(channel_accounts={"webchat": {"x": {"botToken": "t"}}})
        assert "webchat" not in cfg["channels"]
        assert cfg.get("bindings", []) == []

    async def test_write_openclaw_config_threads_channel_accounts(self, tmp_path):
        out = tmp_path / "openclaw.json"
        await write_openclaw_config(
            config_path=out,
            gateway_token="t",
            provider_choice="bedrock_claude",
            user_id="u_1",
            aip_arns=_AIPS,
            channel_accounts={"telegram": {"agent_a": {"botToken": "123:ABC"}}},
        )
        cfg = json.loads(out.read_text())
        assert cfg["channels"]["telegram"]["accounts"]["agent_a"]["botToken"] == "123:ABC"
        assert {
            "type": "route",
            "agentId": "agent_a",
            "match": {"channel": "telegram", "accountId": "agent_a"},
        } in cfg["bindings"]


class TestCronStorePin:
    """cron.store pins the state-DB cron row key to its historical resolved
    value so relocating the state root off EFS (O6) cannot re-key the store
    and orphan every existing cron row. Byte-equal to the derived default =>
    a no-op on the EFS layout."""

    def test_cron_store_not_emitted_for_paid_providers(self):
        """2026.8.1 retired ``cron.store``: job state moved into the state DB and
        the key is no longer in the strict CronSchema. ``cron.enabled`` -- the
        part that actually gates the service -- is unchanged."""
        for provider in ("bedrock_claude", "chatgpt_oauth"):
            cfg = _cfg(provider)
            assert "store" not in cfg["cron"], provider
            assert "enabled" not in cfg["cron"], provider

    def test_trial_cron_disabled(self):
        cfg = _cfg("bedrock_trial", aip_arns={"sonnet": _AIPS["sonnet"]})
        assert cfg["cron"]["enabled"] is False
        assert "store" not in cfg["cron"]
