"""Tests for the cost/behavior tuning keys in the generated openclaw.json.

Pins the config-only adoptions from the 2026-07-17 upstream-docs audit:
heartbeat isolation, subagent model/timeout pinning, cache-aligned context
pruning, skipBootstrap, the pricing-fetch kill switch, utility/llm-task
cheap-tier routing, and the explicit compaction safeguard mode.

Every key asserted here was schema-verified against the deployed fork tag
(v2026.7.1) — OpenClaw's strict zod schemas reject unknown keys at boot and
the container crash-loops, so these tests are the fence against both typos
and silent removal.
"""

from core.containers.config import build_openclaw_config_dict

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}

_SONNET_REF = f"amazon-bedrock/{_AIPS['sonnet']}"
_HAIKU_REF = f"amazon-bedrock/{_AIPS['haiku']}"


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


_ALL_PATHS = (
    {"provider_choice": "bedrock_claude"},
    {"provider_choice": "bedrock_trial"},
    {"provider_choice": "chatgpt_oauth"},
    {"provider_choice": "byo_key", "byo_provider": "anthropic"},
    {"provider_choice": "byo_key", "byo_provider": "openai"},
)


class TestSkipBootstrap:
    def test_skip_bootstrap_true_on_every_path(self):
        """Isol8 pre-seeds IDENTITY/SOUL/USER (friday_identity); without
        skipBootstrap any under-seeded workspace autonomously runs the
        BOOTSTRAP.md identity ritual — token spend + bizarre headless UX."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            assert config["agents"]["defaults"]["skipBootstrap"] is True, kwargs


class TestPricingFetchDisabled:
    def test_models_pricing_disabled_on_every_path(self):
        """2026.8.1 removed models.pricing (pricing now ships with the hosted
        model catalog, so there is no background OpenRouter/LiteLLM fetch to
        disable). The original concern -- a catalog fetch after every boot,
        pure waste and egress on static gate-priced catalogs -- is resolved
        upstream rather than by config, so the key must simply be absent."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            assert "pricing" not in config["models"], kwargs


class TestCompactionMode:
    def test_compaction_mode_pinned_safeguard_everywhere(self):
        """Upstream's default flipped to safeguard for NEW configs only —
        unset means version-dependent drift across image bumps. Pin it."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            assert config["agents"]["defaults"]["compaction"]["mode"] == "safeguard", kwargs

    def test_compaction_cheap_routing_unchanged(self):
        """The mode pin must not disturb the existing cheap-tier routing:
        present on paid Bedrock, absent on the Codex path (documented no-op
        there — doctor deletes it)."""
        paid = _cfg("bedrock_claude")
        assert paid["agents"]["defaults"]["compaction"]["model"] == _HAIKU_REF
        oauth = _cfg("chatgpt_oauth")
        assert "model" not in oauth["agents"]["defaults"]["compaction"]

    def test_compaction_timeout_raised_on_every_path(self):
        """Upstream's 180s default assumes sessions that compact while still
        small. Long-lived heartbeat sessions only hit the threshold once the
        transcript is huge, and the Codex path must summarize on the primary
        model — so 180s kills the run, and the unchanged session kills every
        run after it. 600s stays under the adapter's 15-minute stall timeout."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            assert config["agents"]["defaults"]["compaction"]["timeoutSeconds"] == 600, kwargs


class TestContextPruningTtl:
    def test_pruning_ttl_aligned_with_cache_retention(self):
        """params.cacheRetention="long" buys 1h cache writes; the pruning
        manual-mode ttl default is 5m, which invalidates that cache early.
        The two must move together."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            defaults = config["agents"]["defaults"]
            assert defaults["params"]["cacheRetention"] == "long", kwargs
            assert defaults["contextPruning"] == {"mode": "cache-ttl", "ttl": "1h"}, kwargs


class TestHeartbeatIsolation:
    def test_heartbeat_isolated_and_light_on_paid_bedrock(self):
        hb = _cfg("bedrock_claude")["agents"]["defaults"]["heartbeat"]
        assert hb["every"] == "30m"
        assert hb["isolatedSession"] is True
        assert hb["lightContext"] is True
        assert hb["model"] == _HAIKU_REF

    def test_heartbeat_isolated_and_light_on_oauth(self):
        hb = _cfg("chatgpt_oauth")["agents"]["defaults"]["heartbeat"]
        assert hb["isolatedSession"] is True
        assert hb["lightContext"] is True
        assert hb["model"] == "openai/gpt-5.4-mini"

    def test_heartbeat_disabled_on_trial(self):
        """Trial must pin every="0m" — the documented upstream disable. An
        ABSENT key is NOT off: upstream falls back to a 30m heartbeat at full
        session context on the trial's single premium AIP."""
        assert _cfg("bedrock_trial")["agents"]["defaults"]["heartbeat"] == {"every": "0m"}


class TestUtilityModel:
    def test_utility_model_routed_cheap_per_path(self):
        """Unset utilityModel falls through to the PRIMARY (premium) model
        for titles/utility calls — verified in fork source, the trim||chain
        ends at resolveAgentEffectiveModelPrimary."""
        assert _cfg("bedrock_claude")["agents"]["defaults"]["utilityModel"] == _HAIKU_REF
        assert _cfg("chatgpt_oauth")["agents"]["defaults"]["utilityModel"] == "openai/gpt-5.4-mini"
        assert (
            _cfg("byo_key", byo_provider="anthropic")["agents"]["defaults"]["utilityModel"]
            == "anthropic/claude-haiku-4-5"
        )
        assert _cfg("byo_key", byo_provider="openai")["agents"]["defaults"]["utilityModel"] == "openai/gpt-5.4-mini"

    def test_utility_model_absent_on_trial(self):
        """Empty string does NOT disable utility calls upstream (falls
        through to primary), and the trial's only invocable model IS the
        primary — omit the key rather than pretend to route it."""
        assert "utilityModel" not in _cfg("bedrock_trial")["agents"]["defaults"]


class TestSubagentDefaults:
    def test_subagents_pinned_to_standard_tier(self):
        """Children must not inherit the caller's model: Friday is pinned to
        the premium tier, so every fan-out would be N× premium spend. Pin
        subagents.model to the standard tier (the defaults primary)."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            defaults = config["agents"]["defaults"]
            assert defaults["subagents"]["model"] == defaults["model"]["primary"], kwargs

    def test_subagents_run_timeout_bounded(self):
        """Verified upstream default is 0 = no spawn-level wall clock; a
        wedged child holds a lane and bills until the 2h stale valve. Align
        with the run fence."""
        for kwargs in _ALL_PATHS:
            config = _cfg(**kwargs)
            assert config["agents"]["defaults"]["subagents"]["runTimeoutSeconds"] == 1800, kwargs

    def test_subagents_existing_keys_preserved(self):
        defaults = _cfg("bedrock_claude")["agents"]["defaults"]
        assert defaults["subagents"]["allowAgents"] == ["*"]
        trial = _cfg("bedrock_trial")["agents"]["defaults"]
        assert trial["subagents"]["maxConcurrent"] == 2


class TestLlmTaskRouting:
    def test_llm_task_pinned_cheap_on_paid_bedrock(self):
        entry = _cfg("bedrock_claude")["plugins"]["entries"]["llm-task"]
        assert entry == {
            "enabled": True,
            "config": {
                "defaultProvider": "amazon-bedrock",
                "defaultModel": _AIPS["haiku"],
            },
            "llm": {
                "allowModelOverride": True,
                "allowedCompletionModels": [f"amazon-bedrock/{_AIPS['haiku']}"],
            },
        }

    def test_llm_task_unpinned_where_no_cheap_tier(self):
        """OAuth (Codex) and trial have no separately-invocable cheap tier:
        the entry stays enable-only so llm-task inherits the agent default."""
        for choice in ("chatgpt_oauth", "bedrock_trial"):
            entry = _cfg(choice)["plugins"]["entries"]["llm-task"]
            assert entry == {"enabled": True}, choice

    def test_llm_task_pinned_on_byo_paths(self):
        anthropic = _cfg("byo_key", byo_provider="anthropic")["plugins"]["entries"]["llm-task"]
        assert anthropic["config"] == {
            "defaultProvider": "anthropic",
            "defaultModel": "claude-haiku-4-5",
        }
        openai = _cfg("byo_key", byo_provider="openai")["plugins"]["entries"]["llm-task"]
        assert openai["config"] == {
            "defaultProvider": "openai",
            "defaultModel": "gpt-5.4-mini",
        }
        for entry, target in (
            (anthropic, "anthropic/claude-haiku-4-5"),
            (openai, "openai/gpt-5.4-mini"),
        ):
            assert entry["llm"] == {
                "allowModelOverride": True,
                "allowedCompletionModels": [target],
            }
