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

import base64
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


class TestToolSearchAlwaysOn:
    """Tool search is unconditional — every owner, every provider choice, with
    or without MCP servers connected at config-write time.

    The catalog is unbounded from the generator's point of view: any owner can
    connect an MCP integration at any moment, and the config written today is
    what the container reads for its whole life. Meta's ads MCP alone measured
    95 tools / ~172k tokens against the live endpoint, so gating on what happens
    to be connected at write time is exactly how an owner ends up carrying a full
    catalog afterwards.
    """

    def test_present_for_every_provider_choice(self):
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("byo_key", {"byo_provider": "openai"}),
            # Inert on Codex (its runtime never reads the key) but harmless, and
            # excluding it only made the condition harder to reason about.
            ("chatgpt_oauth", {}),
            # Trial owners can connect integrations too, and a $3 cap is the
            # least forgiving place to carry a full catalog.
            ("bedrock_trial", {}),
        ):
            cfg = _cfg(choice, integration_mcp_servers=_MCP, **kwargs)
            assert cfg["tools"]["toolSearch"] == {"mode": "tools"}, choice

    def test_present_even_with_no_mcp_servers(self):
        """No servers today does not mean no servers tomorrow — the owner can
        connect one without the config being regenerated."""
        assert _cfg("bedrock_claude")["tools"]["toolSearch"] == {"mode": "tools"}
        assert _cfg("chatgpt_oauth")["tools"]["toolSearch"] == {"mode": "tools"}
        assert _cfg("bedrock_trial")["tools"]["toolSearch"] == {"mode": "tools"}

    def test_manual_mcp_servers_are_covered_too(self):
        custom = {"my-internal-api": {"command": "node", "args": ["server.js"], "env": {}}}
        assert _cfg("bedrock_claude", integration_mcp_servers=custom)["tools"]["toolSearch"] == {"mode": "tools"}

    def test_provisioned_mode_is_the_shared_constant(self):
        """Provision-time and the provider swap share one constant, so they
        cannot drift to different modes. This pins the generator to it; the
        swap side is covered by test_billing_change_provider, which asserts the
        kwarg it passes."""
        from core.containers.config import TOOL_SEARCH_MODE

        assert _cfg("bedrock_claude")["tools"]["toolSearch"] == {"mode": TOOL_SEARCH_MODE}


class TestTranscriptRedaction:
    """Paperclip keys must be redacted from persisted transcripts.

    Upstream already runs transcript redaction (mode defaults to "tools",
    applied on the session append path) but its default vendor prefixes cover
    sk-/ghp_/github_pat_/gho_/ghu_ and NOT Paperclip's `pcp_`. A live key
    reached an owner's transcript on 2026-08-14 through exactly that gap.
    """

    def test_paperclip_key_pattern_is_configured_for_every_provider_choice(self):
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
            ("chatgpt_oauth", {}),
            ("bedrock_trial", {}),
        ):
            patterns = _cfg(choice, **kwargs)["logging"]["redactPatterns"]
            assert any("pcp_" in p for p in patterns), choice

    def test_the_pattern_matches_every_shape_the_leak_actually_took(self):
        import re

        pattern = next(p for p in _cfg("bedrock_claude")["logging"]["redactPatterns"] if "pcp_" in p)
        compiled = re.compile(pattern)
        key = "pcp_e7cb205d2877971e2aef28e674fc886579317e12cdbcf3fe"
        # The observed leak was PROSE in the model's reasoning, which is why
        # the default KEY=value pattern did not catch it.
        assert compiled.search(f"token: {key}")
        # And the shapes an `env` dump or a pasted header would produce.
        assert compiled.search(f"PAPERCLIP_API_KEY={key}")
        assert compiled.search(f"Authorization: Bearer {key}")
        assert compiled.search(f'{{"token":"{key}"}}')

    def test_the_pattern_leaves_a_bare_prefix_mention_alone(self):
        import re

        pattern = next(p for p in _cfg("bedrock_claude")["logging"]["redactPatterns"] if "pcp_" in p)
        # Documentation prose about the prefix is not a credential.
        assert not re.compile(pattern).search("keys start with the pcp_ prefix")


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
        env var — which is exactly what changed when the live view set DISPLAY
        on the task definition. Setting it explicitly is what makes that a
        deliberate flip rather than a silent one.

        Applies to every provider path — the browser is not provider-specific.
        """
        for choice, kwargs in (
            ("bedrock_claude", {}),
            ("bedrock_trial", {}),
            ("chatgpt_oauth", {}),
            ("byo_key", {"byo_provider": "anthropic"}),
        ):
            browser = _cfg(choice, **kwargs)["browser"]
            # Now False fleet-wide: the live view runs Chrome headful on the
            # Xvfb display. Still asserted explicitly rather than left implicit,
            # which is the point the docstring above makes.
            # See test_browser_is_headful_in_every_environment.
            assert browser["headless"] is False, choice
            assert browser["noSandbox"] is True, choice
            # Real Chrome (layer 9b), NOT /usr/bin/chromium — that symlink is
            # Playwright's Chrome-for-Testing, whose permanent "automated
            # testing only" banner faced the owner in the live-view panel.
            assert browser["executablePath"] == "/usr/bin/google-chrome", choice
            # Schema caps both at 120_000; defaults (15s/8s) are the ones
            # upstream says to raise on slow storage.
            assert 15_000 < browser["localLaunchTimeoutMs"] <= 120_000, choice
            assert 8_000 < browser["localCdpReadyTimeoutMs"] <= 120_000, choice

    def test_browser_is_headful_in_every_environment(self):
        """Containers run headful on the Xvfb display so the browser can be
        watched and driven from the workspace panel.

        Deliberately NOT environment-dependent: the matching DISPLAY on the task
        definition (container-stack.ts) is unconditional too, and the pair must
        agree everywhere. Upstream's resolveManagedBrowserHeadlessMode evaluates
        its linux-no-DISPLAY branch BEFORE this value, so headless=False without
        DISPLAY is silently forced back to headless, and DISPLAY without
        headless=False is an Xvfb nobody ever draws on. Either half alone is a
        no-op; only the pair does anything.

        Asserted across environments so a future env-gate here cannot land
        without also gating DISPLAY, which would silently disable the live view
        in whichever environment was missed.
        """
        for env in ("dev", "prod", "staging", "local", ""):
            with patch.object(config_module.settings, "ENVIRONMENT", env):
                assert _cfg()["browser"]["headless"] is False, env

    def test_no_crash_reporter_flags(self):
        """Regression: --disable-breakpad + --crash-dumps-dir were added (#1594)
        as a fix for Chrome's `chrome_crashpad_handler: --database is required`
        abort, then tested in the prod container with OpenClaw's exact argv —
        byte-identical failure with the flags present. The cause was an
        unwritable XDG_CONFIG_HOME (root-owned on EFS vs the access point's
        uid 1000), fixed in Workspace.ensure_xdg_dirs. These flags do nothing
        for a launch failure; do not re-add them for one.

        Narrowed from "extraArgs must be absent" when the WebGL flags landed:
        the ban was always about these two flags, not the key.
        """
        extra_args = _cfg()["browser"].get("extraArgs", [])
        crash_reporter_tokens = ("breakpad", "crashpad", "crash-reporter", "crash-dumps-dir")
        assert not [a for a in extra_args if any(t in a for t in crash_reporter_tokens)]

    def test_webgl_flags_present(self):
        """WebGL must work AND the live-view must not be throttled to ~4fps.

        Chrome 137 removed the automatic SwiftShader fallback and prod runs
        Chrome 151 on GPU-less Fargate, so without a software renderer the
        container has NO WebGL — `canvas.getContext("webgl")` returns null, a
        state essentially no real desktop is in, and Arkose/FunCaptcha (LinkedIn
        sign-in) renders its 3D puzzle only with WebGL. `--enable-unsafe-swiftshader`
        hands WebGL surfaces an in-process SwiftShader context and is kept.

        Compositing is a separate axis. `--use-angle=swiftshader` routes ALL GL
        (incl. 2D page compositing the live-view captures) through
        ANGLE->Vulkan->SwiftShader and was measured 2026-08-20 to cap the
        live-view at ~4fps; `--disable-gpu` (native Skia software compositor) is
        ~8x faster at the same CPU with WebGL preserved. So we ship --disable-gpu
        and BAN both --use-angle=swiftshader (slow compositor) and --use-gl=angle
        (broken llvmpipe path — "BindToCurrentSequence failed").
        """
        extra_args = _cfg()["browser"]["extraArgs"]
        # WebGL preserved via in-process SwiftShader fallback.
        assert "--enable-unsafe-swiftshader" in extra_args
        # Fast native software compositor (not ANGLE) — the ~8x live-view win.
        assert "--disable-gpu" in extra_args
        # Never re-add the slow ANGLE-SwiftShader compositor or the broken
        # llvmpipe path.
        assert "--use-angle=swiftshader" not in extra_args
        assert "--use-angle=gl" not in extra_args
        assert "--use-gl=angle" not in extra_args

    def test_browser_egress_proxy_off_by_default(self):
        """No knob, no proxy flag — a container keeps egressing on the AWS NAT
        IP exactly as before. The container image ships WARP inert, so an
        accidental flag here would point Chrome at a dead loopback port fleet-
        wide.
        """
        with patch.object(config_module.settings, "BROWSER_EGRESS_PROXY_PORT", 0):
            assert not [a for a in _cfg()["browser"]["extraArgs"] if "proxy" in a]

    def test_browser_egress_proxy_flag_when_enabled(self):
        """When on: exactly one flag, a data: PAC that routes through the WARP
        loopback proxy and — load-bearing — falls back to DIRECT.

        The `; DIRECT` tail is the entire reason this is a PAC rather than
        `--proxy-server=socks5://…`. Verified against a real WARP proxy
        2026-08-21: with it, killing warp-svc still loads the page (origin IP,
        0.6s); without it, the load fails outright. A "simplification" that
        drops it turns any container whose warp-svc did not connect into one
        with no working browser at all.
        """
        with patch.object(config_module.settings, "BROWSER_EGRESS_PROXY_PORT", 40000):
            flags = [a for a in _cfg()["browser"]["extraArgs"] if "proxy" in a]
        assert len(flags) == 1, flags
        prefix = "--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,"
        assert flags[0].startswith(prefix)
        pac = base64.b64decode(flags[0][len(prefix) :]).decode()
        assert "SOCKS5 127.0.0.1:40000; DIRECT" in pac
        # Loopback is never proxied — CDP, the X display and the gateway all
        # live there. Assert the actual match conditions, not just that some
        # DIRECT branch exists (the `; DIRECT` fallback above is one too).
        assert 'if(isPlainHostName(h)||shExpMatch(h,"127.*")||h=="::1")return "DIRECT";' in pac

    def test_launch_budget_stays_below_the_tool_timeout(self):
        """The launcher's own error must win the race against the tool-level
        timeout, or the CDP diagnostic and Chrome stderr are lost.

        Setting localLaunchTimeoutMs to 60s (== BROWSER_ACTION_TIMEOUT_MS in
        browser/constants.ts) turned a diagnosable `Failed to start Chrome CDP
        ... http_unreachable ... chrome_crashpad_handler: --database is
        required` into a bare `browser failed: timed out`, which is how this
        bug stayed unexplained. Keep a margin.
        """
        assert _cfg()["browser"]["localLaunchTimeoutMs"] <= 40_000


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

    def test_search_pinned_on_chatgpt_oauth_too(self):
        """Regression: chatgpt_oauth used to be left unpinned so that
        "Codex-native hosted search" could engage. Those owners run the
        EMBEDDED runtime (we never set agents.defaults.embeddedHarness.runtime),
        which has no hosted search, so the exception left them with a
        web_search tool that had no provider and threw on every call — and no
        fallback, since firecrawl_search needs an API key we never set.

        If chatgpt_oauth is ever genuinely moved onto the Codex runtime, change
        the runtime first; this assertion is what should force that discussion.
        """
        assert _cfg("chatgpt_oauth")["tools"]["web"]["search"] == {"provider": "parallel-free"}


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
