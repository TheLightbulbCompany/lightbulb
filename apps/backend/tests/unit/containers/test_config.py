"""Tests for OpenClaw container config generation."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


from core.containers.config import (
    OPENCLAW_UPSTREAM_VERSION,
    build_openclaw_config_dict,
    write_openclaw_config,
    merge_openclaw_config,
    _deep_merge,
)


def test_openclaw_upstream_version_matches_pinned_json():
    """OPENCLAW_UPSTREAM_VERSION (used in the meta shim that defeats
    openclaw's auto-restore) must stay in sync with openclaw-version.json#tag.
    The shim works as long as the value is a non-empty string that parses
    as a valid openclaw version, but drift would surface a "Config was last
    written by a newer OpenClaw" warning at runtime — so we lock it down
    here. Drift in either direction breaks CI."""
    # apps/backend/tests/unit/containers/test_config.py -> repo root is parents[5]
    repo_root = Path(__file__).resolve().parents[5]
    pinned = json.loads((repo_root / "openclaw-version.json").read_text())
    assert OPENCLAW_UPSTREAM_VERSION == pinned["tag"], (
        f"core/containers/config.py::OPENCLAW_UPSTREAM_VERSION "
        f"({OPENCLAW_UPSTREAM_VERSION!r}) is out of sync with "
        f"openclaw-version.json#tag ({pinned['tag']!r}). Update both."
    )


def _cfg(provider_choice: str = "bedrock_claude", **kwargs) -> dict:
    """Build a config dict with sensible defaults for the per-section tests."""
    defaults = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
    }
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


class TestOpenclawConfigShape:
    """Verifies the static sections OpenClaw refuses to start without.

    These cover the regression PR #391 introduced when ``write_openclaw_config``
    was reduced to a 5-line stub — gateway/memory/tools/hooks/channels/browser
    must always be present regardless of provider_choice.
    """

    def test_gateway_token_auth(self):
        config = _cfg(gateway_token="test-token-123")
        assert config["gateway"]["mode"] == "local"
        assert config["gateway"]["bind"] == "lan"
        assert config["gateway"]["auth"] == {"mode": "token", "token": "test-token-123"}
        assert config["gateway"]["trustedProxies"] == ["10.0.0.0/8", "127.0.0.1", "::1"]

    def test_control_ui_disabled(self):
        config = _cfg()
        assert config["gateway"]["controlUi"]["enabled"] is False

    def test_chat_completions_disabled(self):
        """We use WebSocket RPC, not Bedrock-compatible chat completions."""
        config = _cfg()
        assert config["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"] is False

    def test_agents_defaults_workspace_routes_to_efs(self):
        config = _cfg()
        assert config["agents"]["defaults"]["workspace"] == "/home/node/.openclaw/workspaces"

    def test_meta_shim_uses_real_upstream_version(self):
        """`meta.lastTouchedVersion` must be the real openclaw version
        (parses as semver in upstream's compareOpenClawVersions); arbitrary
        strings work for hasConfigMeta but trigger spurious "from future"
        warnings in warnIfConfigFromFuture (io.ts:885)."""
        config = _cfg()
        assert config["meta"]["lastTouchedVersion"] == OPENCLAW_UPSTREAM_VERSION

    def test_agents_defaults_memory_search_pinned_to_bedrock(self):
        # Embedding provider must be pinned to bedrock — the upstream
        # default is "local" which needs a GGUF model file we don't ship,
        # and qmd's first embed cycle hangs ~3min on that. Bedrock works
        # via task-role IAM regardless of the user's chosen auth path.
        config = _cfg()
        memory_search = config["agents"]["defaults"]["memorySearch"]
        assert memory_search["enabled"] is True
        assert memory_search["provider"] == "bedrock"
        assert memory_search["model"] == "amazon.titan-embed-text-v2:0"

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
        main_entry = next(a for a in config["agents"]["list"] if a.get("id") == "main")
        assert main_entry["workspace"] == "/home/node/.openclaw/workspaces/main"
        assert main_entry["default"] is True
        assert main_entry["reasoningDefault"] == "stream"

    def test_memory_builtin_backend(self):
        """qmd backend was removed 2026-05-02 (EFS NFS deadlock class).
        Builtin = flat MEMORY.md files in agent workspace, no sqlite."""
        config = _cfg()
        memory = config["memory"]
        assert memory["backend"] == "builtin"
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

    def test_tools_exec_approval_policy(self):
        """allowlist + on-miss lets the in-chat approval card decide on
        unknown commands. OpenClaw's default security=deny silently blocks
        every exec call (exec-defaults.ts:98)."""
        config = _cfg()
        exec_cfg = config["tools"]["exec"]
        assert exec_cfg["security"] == "allowlist"
        assert exec_cfg["ask"] == "on-miss"

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

    def test_channels_telegram_discord_slack_enabled(self):
        """Plugins must be loaded at provision time so subsequent
        token/account changes are a fast per-channel restart, not a
        gateway-wide restart (~6 min on Fargate)."""
        config = _cfg()
        for ch in ("telegram", "discord", "slack"):
            assert config["channels"][ch]["enabled"] is True
            assert config["channels"][ch]["dmPolicy"] == "pairing"

    def test_session_dm_scope(self):
        config = _cfg()
        assert config["session"]["dmScope"] == "per-account-channel-peer"

    def test_browser_two_profiles_default_openclaw(self):
        """Both profiles are statically defined; default is the cloud
        Chromium profile so unbound sessions (channels, cron, unpaired
        UI) don't try to hit a non-existent desktop. The `user` profile
        only routes when a desktop node is paired."""
        config = _cfg()
        browser = config["browser"]
        assert browser["enabled"] is True
        assert browser["defaultProfile"] == "openclaw"
        # Container Chromium (bundled in the extended image since PR #596).
        assert browser["profiles"]["openclaw"]["driver"] == "openclaw"
        assert isinstance(browser["profiles"]["openclaw"]["color"], str)
        # cdpPort required by upstream schema since v2026.5.7-0d10b38:
        # zod-schema.ts:483 refines any non-`existing-session` profile
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
        assert isinstance(browser["profiles"]["user"]["color"], str)
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
        """The three owner-operated chat commands (/mcp, /plugins, /debug)
        are off-by-default upstream. We flip them on for per-user Isol8
        containers because the owner needs to manage their own container
        from chat. /config and /bash stay off — broad config writes
        belong to the control panel + config_patcher, and host-shell
        access from chat widens the trust boundary too far given
        channel-linked accounts."""
        config = _cfg()
        assert config["commands"]["mcp"] is True
        assert config["commands"]["plugins"] is True
        assert config["commands"]["debug"] is True
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
        ],
    )
    def test_channels_loaded_for_every_provider(self, provider_choice, kwargs):
        config = _cfg(provider_choice=provider_choice, **kwargs)
        assert config["channels"]["telegram"]["enabled"] is True
        assert config["channels"]["discord"]["enabled"] is True
        assert config["channels"]["slack"]["enabled"] is True


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
        )
        cfg = json.loads(out.read_text())
        # Sanity-check one field from each major section.
        assert cfg["gateway"]["auth"]["token"] == "t-1"
        assert cfg["agents"]["defaults"]["verboseDefault"] == "full"
        assert cfg["memory"]["backend"] == "builtin"
        assert cfg["tools"]["exec"]["security"] == "allowlist"
        assert cfg["channels"]["telegram"]["enabled"] is True
        assert cfg["browser"]["defaultProfile"] == "openclaw"
        assert "nodeHost" not in cfg
        assert cfg["gateway"]["nodes"]["browser"]["mode"] == "auto"

    async def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "openclaw.json"
        await write_openclaw_config(
            config_path=nested,
            gateway_token="t",
            provider_choice="bedrock_claude",
            user_id="u_1",
        )
        assert nested.exists()


class TestPatchOpenclawConfig:
    """Test config patching/merging."""

    def test_shallow_override(self):
        base = {"gateway": {"mode": "local"}, "browser": {"enabled": False}}
        patch = {"browser": {"enabled": True}}
        result = merge_openclaw_config(base, patch)
        assert result["browser"]["enabled"] is True
        assert result["gateway"]["mode"] == "local"

    def test_deep_merge_nested(self):
        base = {
            "tools": {
                "web": {"fetch": {"enabled": False, "timeout": 30}},
                "media": {"image": {"enabled": False}},
            }
        }
        patch = {
            "tools": {
                "web": {"fetch": {"enabled": True}},
            }
        }
        result = merge_openclaw_config(base, patch)
        assert result["tools"]["web"]["fetch"]["enabled"] is True
        assert result["tools"]["web"]["fetch"]["timeout"] == 30
        assert result["tools"]["media"]["image"]["enabled"] is False

    def test_new_key_added(self):
        base = {"gateway": {"mode": "local"}}
        patch = {"newSection": {"key": "value"}}
        result = merge_openclaw_config(base, patch)
        assert result["newSection"]["key"] == "value"

    def test_original_not_mutated(self):
        base = {"gateway": {"mode": "local"}}
        patch = {"gateway": {"mode": "remote"}}
        result = merge_openclaw_config(base, patch)
        assert result["gateway"]["mode"] == "remote"
        assert base["gateway"]["mode"] == "local"


class TestDeepMerge:
    """Test _deep_merge helper."""

    def test_flat_merge(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_value(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 3, "d": 4}}
        result = _deep_merge(base, override)
        assert result == {"a": {"b": 1, "c": 3, "d": 4}}

    def test_non_dict_override(self):
        assert _deep_merge({"a": {"b": 1}}, {"a": "string"}) == {"a": "string"}


# Deterministic Ed25519 public key in PEM form — same fixture used in
# test_desktop_pair.py. The derived deviceId is stable across runs.
_FIXTURE_PUBKEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAO2onvM62pC1io6jQKm8Nc2UyFXcd4kOmOsBIoYtZ2ik=
-----END PUBLIC KEY-----
"""
_FIXTURE_EXPECTED_DEVICE_ID = "139e3940e64b5491722088d9a0d741628fc826e09475d341a780acde3c4b8070"


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
        assert entry["publicKey"] == _FIXTURE_PUBKEY_PEM
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
