"""Tests for OpenClaw container config generation."""

import json
from pathlib import Path

import pytest


from core.containers.config import (
    OPENCLAW_UPSTREAM_VERSION,
    build_openclaw_config_dict,
    write_openclaw_config,
    write_mcporter_config,
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

    def test_browser_user_profile(self):
        """Default profile attaches to the user's real Chrome via CDP."""
        config = _cfg()
        browser = config["browser"]
        assert browser["enabled"] is True
        assert browser["defaultProfile"] == "user"
        assert browser["profiles"]["user"]["driver"] == "existing-session"
        # color is required since OpenClaw's schema bump.
        assert isinstance(browser["profiles"]["user"]["color"], str)

    def test_node_host_browser_proxy_enabled(self):
        """Auto-routes browser tool calls to the paired desktop node."""
        config = _cfg()
        assert config["nodeHost"]["browserProxy"]["enabled"] is True

    def test_update_check_disabled(self):
        config = _cfg()
        assert config["update"]["checkOnStart"] is False

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
        assert cfg["browser"]["defaultProfile"] == "user"
        assert cfg["nodeHost"]["browserProxy"]["enabled"] is True

    async def test_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "openclaw.json"
        await write_openclaw_config(
            config_path=nested,
            gateway_token="t",
            provider_choice="bedrock_claude",
            user_id="u_1",
        )
        assert nested.exists()


class TestWriteMcporterConfig:
    """Test mcporter.json generation."""

    def test_generates_valid_json(self):
        result = write_mcporter_config()
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_default_empty_servers(self):
        config = json.loads(write_mcporter_config())
        assert config["servers"] == {}

    def test_custom_servers(self):
        servers = {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test"},
            }
        }
        config = json.loads(write_mcporter_config(servers=servers))
        assert "github" in config["servers"]
        assert config["servers"]["github"]["command"] == "npx"
        assert config["servers"]["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test"

    def test_none_servers_returns_empty(self):
        config = json.loads(write_mcporter_config(servers=None))
        assert config["servers"] == {}


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
