"""Unit tests for Bedrock Claude pricing constants and cost calc."""

import pytest

from core.billing.bedrock_pricing import (
    UnknownModelError,
    cost_microcents,
    get_all_rates,
    get_rate,
    normalize_model_id,
)


class TestCostMicrocents:
    def test_sonnet_4_6_cost(self):
        # Sonnet 4.6: $3 / MTok input, $15 / MTok output (Bedrock list price).
        # 1 MTok = 1,000,000 tokens. $1 = 100 cents = 1,000,000 microcents.
        # 1000 input + 500 output should cost:
        # (1000 / 1_000_000) * $3 = $0.003 = 3000 microcents (input)
        # (500 / 1_000_000) * $15 = $0.0075 = 7500 microcents (output)
        # Total: 10500 microcents
        result = cost_microcents(
            model_id="anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )
        assert result == 10_500

    def test_opus_4_6_cost(self):
        # Opus 4.6: $15 / MTok input, $75 / MTok output.
        # 1000 input + 500 output:
        # (1000 / 1_000_000) * $15 = $0.015 = 15_000 microcents (input)
        # (500 / 1_000_000) * $75 = $0.0375 = 37_500 microcents (output)
        # Total: 52_500 microcents
        result = cost_microcents(
            model_id="anthropic.claude-opus-4-6-v1",
            input_tokens=1000,
            output_tokens=500,
        )
        assert result == 52_500

    def test_haiku_4_5_cost(self):
        # Haiku 4.5: $1 / MTok input (the cheap aux-task tier).
        # 1 MTok input + 0 output:
        # (1_000_000 / 1_000_000) * $1 = $1 = 1_000_000 microcents
        result = cost_microcents(
            model_id="anthropic.claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert result == 1_000_000

    def test_unknown_model_raises(self):
        with pytest.raises(UnknownModelError) as exc:
            cost_microcents(model_id="anthropic.claude-fake-99", input_tokens=100, output_tokens=100)
        assert "anthropic.claude-fake-99" in str(exc.value)

    def test_zero_tokens_zero_cost(self):
        result = cost_microcents(
            model_id="anthropic.claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
        )
        assert result == 0

    def test_cost_is_integer(self):
        """Microcents are integers - no float drift in the deduct path."""
        result = cost_microcents(
            model_id="anthropic.claude-sonnet-4-6",
            input_tokens=1234,
            output_tokens=5678,
        )
        assert isinstance(result, int)

    def test_cache_read_discount(self):
        """cache_read tokens bill at 0.1× input rate ($0.30/MTok for Sonnet)."""
        result = cost_microcents(
            model_id="anthropic.claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=10_000,
        )
        # 10_000 * $0.30/MTok = $0.003 = 3000 microcents
        assert result == 3000

    def test_cache_write_surcharge(self):
        """cache_write tokens bill at 1.25× input rate ($3.75/MTok for Sonnet)."""
        result = cost_microcents(
            model_id="anthropic.claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_write_tokens=10_000,
        )
        # 10_000 * $3.75/MTok = $0.0375 = 37_500 microcents
        assert result == 37_500

    def test_cache_tokens_default_to_zero(self):
        """Older callers that don't pass cache tokens get the same answer."""
        without_cache = cost_microcents(
            model_id="anthropic.claude-opus-4-6-v1",
            input_tokens=1000,
            output_tokens=500,
        )
        with_zero_cache = cost_microcents(
            model_id="anthropic.claude-opus-4-6-v1",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        assert without_cache == with_zero_cache


class TestGetAllRates:
    def test_returns_both_models(self):
        rates = get_all_rates()
        assert "anthropic.claude-sonnet-4-6" in rates
        assert "anthropic.claude-opus-4-6-v1" in rates

    def test_each_rate_has_all_four_fields(self):
        rates = get_all_rates()
        for model_id, rate in rates.items():
            assert {"input", "output", "cache_read", "cache_write"} <= set(rate.keys()), model_id

    def test_returns_defensive_copy(self):
        """Mutating the result must not affect future calls."""
        rates = get_all_rates()
        rates["anthropic.claude-sonnet-4-6"]["input"] = 999.0
        fresh = get_all_rates()
        assert fresh["anthropic.claude-sonnet-4-6"]["input"] == 3.0


class TestGetRate:
    def test_known_model(self):
        rate = get_rate("anthropic.claude-opus-4-6-v1")
        assert rate["input"] == 15.0
        assert rate["output"] == 75.0

    def test_haiku_4_5_rate(self):
        rate = get_rate("anthropic.claude-haiku-4-5")
        assert rate["input"] == 1.0
        assert rate["output"] == 5.0
        assert rate["cache_read"] == 0.1
        assert rate["cache_write"] == 1.25

    def test_haiku_4_5_inference_profile_id_normalizes(self):
        """The full openclaw ref must collapse to the same Haiku entry."""
        assert get_rate("amazon-bedrock/us.anthropic.claude-haiku-4-5") == get_rate(
            "anthropic.claude-haiku-4-5"
        )

    def test_unknown_model_raises(self):
        with pytest.raises(UnknownModelError):
            get_rate("anthropic.claude-fake-99")


class TestNormalizeModelId:
    """Bedrock 4.x ids are INFERENCE_PROFILE-only; the gateway emits
    region-prefixed ids (``us.``/``global.``/etc.) that must collapse to
    the bare foundation-model id used as the rate-table key."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Already bare — idempotent.
            ("anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
            # Provider prefix only.
            ("amazon-bedrock/anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
            # Inference-profile prefix only.
            ("us.anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
            ("global.anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
            ("eu.anthropic.claude-opus-4-6-v1", "anthropic.claude-opus-4-6-v1"),
            # Both prefixes — the actual shape emitted by chat.final.
            ("amazon-bedrock/us.anthropic.claude-sonnet-4-6", "anthropic.claude-sonnet-4-6"),
            ("amazon-bedrock/global.anthropic.claude-opus-4-6-v1", "anthropic.claude-opus-4-6-v1"),
        ],
    )
    def test_strips_to_bare_id(self, raw, expected):
        assert normalize_model_id(raw) == expected

    def test_get_rate_accepts_inference_profile_id(self):
        """get_rate must look up the bare id, not the prefixed one."""
        rate = get_rate("us.anthropic.claude-sonnet-4-6")
        assert rate["input"] == 3.0
        assert rate["output"] == 15.0

    def test_get_rate_accepts_full_openclaw_ref(self):
        rate = get_rate("amazon-bedrock/us.anthropic.claude-opus-4-6-v1")
        assert rate["input"] == 15.0
        assert rate["output"] == 75.0

    def test_cost_microcents_with_inference_profile_id(self):
        """cost_microcents inherits get_rate's normalization."""
        result = cost_microcents(
            model_id="amazon-bedrock/us.anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=500,
        )
        # Same answer as the bare-id case in TestCostMicrocents above.
        assert result == 10_500
