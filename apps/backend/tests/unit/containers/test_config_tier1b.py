"""Tier-1b adoptions + security/governance config keys (2026-07-17 audit).

Covers: gated tool-search, browser snapshot/tab bounds, firecrawl fetch
fallback, per-path web-search provider, the widened tools.deny (tts/media-gen)
with its node_proxy sync contract, the compaction-notifier hook, progress
drafts on re-injected channels, prod-gated /debug, hooks agent-id allowlist,
and session maintenance/reset bounds.

Every key was schema-verified against the deployed fork tag (v2026.7.1) —
unknown keys crash-loop the container at boot. `sshVerify` and channel
`streaming.progress.commandText` were verified ABSENT at that tag and are
deliberately not emitted (deferred to the next image bump).
"""

from unittest.mock import patch

from core.containers import config as config_module
from core.containers.config import (
    TOOL_DENY_BASE,
    TOOL_DENY_WITH_NODES,
    build_openclaw_config_dict,
)

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}

_MCP = {"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"], "env": {}}}


def _cfg(provider_choice: str = "bedrock_claude", **kwargs) -> dict:
    defaults = {
        "user_id": "u_1",
        "gateway_token": "test-token",
        "provider_choice": provider_choice,
    }
    if provider_choice in ("bedrock_claude", "bedrock_trial"):
        defaults["aip_arns"] = _AIPS if provider_choice == "bedrock_claude" else {"sonnet": _AIPS["sonnet"]}
    defaults.update(kwargs)
    return build_openclaw_config_dict(**defaults)


class TestToolSearchGated:
    def test_enabled_for_mcp_heavy_embedded_runtime_owners(self):
        """Tool search pays off only when merged MCP servers inflate the tool
        catalog, and only on the embedded runtime (Codex has its own)."""
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("byo_key", {"byo_provider": "openai"}),
        ):
            cfg = _cfg(choice, integration_mcp_servers=_MCP, **kwargs)
            assert cfg["tools"]["toolSearch"] == {"mode": "code"}, choice

    def test_absent_without_mcp_servers(self):
        assert "toolSearch" not in _cfg("bedrock_claude")["tools"]

    def test_manual_mcp_servers_also_trigger_the_gate(self):
        """DELIBERATE (Codex P2 follow-up): the gate keys on ANY merged MCP
        server — registry integration or user-added custom server — because
        every server contributes schemas of unknowable size at config-write
        time and churns the cached prompt prefix. Erring toward enablement
        for MCP users is the chosen posture; plain owners stay direct."""
        custom = {"my-internal-api": {"command": "node", "args": ["server.js"], "env": {}}}
        cfg = _cfg("bedrock_claude", integration_mcp_servers=custom)
        assert cfg["tools"]["toolSearch"] == {"mode": "code"}

    def test_absent_on_codex_and_trial(self):
        """Codex runtime ignores tools.toolSearch (native tool search); trial
        keeps the minimal surface."""
        assert "toolSearch" not in _cfg("chatgpt_oauth", integration_mcp_servers=_MCP)["tools"]
        assert "toolSearch" not in _cfg("bedrock_trial", integration_mcp_servers=_MCP)["tools"]


class TestBrowserBounds:
    def test_efficient_snapshots_and_tab_cleanup(self):
        """Upstream tab defaults (8 tabs / 120m idle) are desktop-sized; on a
        1 vCPU / 4 GB task headless Chromium tabs are still a resource constraint."""
        browser = _cfg()["browser"]
        assert browser["snapshotDefaults"] == {"mode": "efficient"}
        assert browser["tabCleanup"] == {"idleMinutes": 30, "maxTabsPerSession": 4}
        # Existing profile wiring untouched.
        assert browser["defaultProfile"] == "openclaw"
        assert browser["profiles"]["openclaw"]["cdpPort"] == 18800

    def test_upstream_documented_linux_recipe_is_applied(self):
        """Upstream's documented Linux fix for "Failed to start Chrome CDP on
        port 18800" (docs/tools/browser-linux-troubleshooting.md) is
        enabled + executablePath + headless + noSandbox. We shipped only two
        of the four, and never raised the timeouts the same doc says to raise
        on slow storage — our profile lives on EFS.

        headless especially: upstream's default is FALSE, and we were relying
        on the implicit "no DISPLAY/WAYLAND_DISPLAY -> fall back to headless"
        path. That makes a load-bearing property depend on the ABSENCE of an
        env var; anything that later sets DISPLAY launches a headful Chrome
        with no X server.

        Applies to every provider path — the browser is not provider-specific.
        """
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
            ("chatgpt_oauth", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
        ):
            browser = _cfg(choice, **kwargs)["browser"]
            assert browser["headless"] is True, choice
            assert browser["noSandbox"] is True, choice
            assert browser["executablePath"] == "/usr/bin/chromium", choice
            # Schema caps both at 120_000; defaults (15s/8s) are the ones
            # upstream says to raise on slow storage.
            assert 15_000 < browser["localLaunchTimeoutMs"] <= 120_000, choice
            assert 8_000 < browser["localCdpReadyTimeoutMs"] <= 120_000, choice


class TestWebFetchAndSearch:
    def test_fetch_gets_keyless_firecrawl_fallback(self):
        for kwargs in ({"provider_choice": "bedrock_claude"}, {"provider_choice": "chatgpt_oauth"}):
            cfg = _cfg(**kwargs)
            assert cfg["tools"]["web"]["fetch"] == {"enabled": True, "provider": "firecrawl"}, kwargs
            assert cfg["plugins"]["entries"]["firecrawl"] == {"enabled": True}, kwargs

    def test_search_pinned_keyless_on_embedded_runtime(self):
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
        ):
            cfg = _cfg(choice, **kwargs)
            assert cfg["tools"]["web"]["search"] == {"provider": "parallel-free"}, choice

    def test_search_unpinned_on_codex_so_hosted_search_engages(self):
        """A pinned managed provider suppresses Codex-native hosted search the
        owner's ChatGPT subscription already includes."""
        assert "search" not in _cfg("chatgpt_oauth")["tools"]["web"]


class TestToolDeny:
    def test_deny_includes_media_egress_tools(self):
        deny = set(_cfg()["tools"]["deny"])
        assert {"canvas", "nodes", "cron", "tts", "music_generate", "video_generate"} <= deny
        # image_generate deliberately NOT denied — chat renders images and the
        # bill is the owner's own credential.
        assert "image_generate" not in deny

    def test_deny_constants_are_the_single_source(self):
        assert set(TOOL_DENY_WITH_NODES) == set(TOOL_DENY_BASE) | {"nodes"}
        assert set(_cfg()["tools"]["deny"]) == set(TOOL_DENY_WITH_NODES)

    def test_node_proxy_uses_shared_constants(self):
        """node_proxy REPLACES the deny list wholesale on node pair/unpair; a
        hand-written list there would silently re-enable tts/media-gen the
        moment a desktop pairs. It must consume the shared constants."""
        import inspect

        from core.services import node_proxy

        src = inspect.getsource(node_proxy)
        assert "TOOL_DENY_BASE" in src
        assert "TOOL_DENY_WITH_NODES" in src
        # No hand-maintained literal deny lists left behind.
        assert '["canvas", "cron"]' not in src
        assert '["canvas", "nodes", "cron"]' not in src


class TestCompactionNotifierHook:
    def test_bundled_notifier_enabled_alongside_existing_hooks(self):
        entries = _cfg()["hooks"]["internal"]["entries"]
        assert entries["compaction-notifier"] == {"enabled": True}
        assert entries["command-logger"] == {"enabled": True}
        assert entries["session-memory"] == {"enabled": True}


class TestChannelProgressDrafts:
    _ACCOUNTS = {"telegram": {"acct_1": {"token": "enc", "allowFrom": ["u"]}}}

    def test_reinjected_telegram_gets_progress_streaming(self):
        cfg = _cfg(channel_accounts=self._ACCOUNTS)
        assert cfg["channels"]["telegram"]["streaming"] == {"mode": "progress"}

    def test_discord_left_on_upstream_default(self):
        """Discord already defaults to progress upstream — no key needed."""
        cfg = _cfg(channel_accounts={"discord": {"acct_1": {"token": "enc"}}})
        assert "streaming" not in cfg["channels"]["discord"]

    def test_trial_still_gets_no_channels(self):
        cfg = _cfg("bedrock_trial", channel_accounts=self._ACCOUNTS)
        assert cfg["channels"] == {}


class TestDebugCommandProdGate:
    def test_debug_off_in_prod(self):
        """/debug set applies runtime overrides to ANY config path — including
        tools.deny (re-arming the fleet-denied cron tool) and the runaway
        bounds. Owner-gated chat is not a sufficient boundary in prod."""
        with patch.object(config_module.settings, "ENVIRONMENT", "prod"):
            assert _cfg()["commands"]["debug"] is False

    def test_debug_stays_on_outside_prod(self):
        for env in ("", "dev", "staging"):
            with patch.object(config_module.settings, "ENVIRONMENT", env):
                assert _cfg()["commands"]["debug"] is True, env


class TestHooksAgentAllowlist:
    def test_allowlist_is_the_seeded_roster(self):
        agents = [
            {"id": "agent_friday", "role": "ceo", "name": "Friday"},
            {"id": "agent_atlas", "role": "recruiter", "name": "Atlas"},
        ]
        cfg = _cfg(paperclip_agents=agents)
        assert cfg["hooks"]["allowedAgentIds"] == ["agent_friday", "agent_atlas"]

    def test_allowlist_falls_back_to_main(self):
        assert _cfg()["hooks"]["allowedAgentIds"] == ["main"]


class TestSessionBounds:
    def test_maintenance_enforce_with_raised_bounds(self):
        """warn mode is unbounded growth on EFS; enforce spares durable human
        conversations (synthetic rows only) and zstd-archives before deleting.
        Bounds are generous multiples of upstream defaults (500 rows / 30d) to
        protect heavy owners; resetArchiveRetention defaults to keep-forever
        upstream, so it must be set explicitly."""
        maintenance = _cfg()["session"]["maintenance"]
        assert maintenance == {
            "mode": "enforce",
            "maxEntries": 2000,
            "pruneAfter": "90d",
            "maxDiskBytes": "500mb",
            "resetArchiveRetention": "90d",
        }

    def test_reset_is_idle_based(self):
        """The unconfigured default is daily 04:00 gateway-local = 04:00 UTC =
        23:00 ET — it rolls a US owner's live evening session. Idle-based
        reset needs no owner-timezone knowledge."""
        assert _cfg()["session"]["reset"] == {"mode": "idle", "idleMinutes": 1440}

    def test_dm_scope_preserved(self):
        assert _cfg()["session"]["dmScope"] == "per-account-channel-peer"
