from core.billing import meta_pricing as mp


def test_cost_input_output_microcents():
    # USD-per-million == microcents-per-token numerically.
    # 1_000_000 input * 1.25/1e6 * 1e6 microcents = 1_250_000 microcents
    assert mp.meta_cost_microcents(1_000_000, 0) == 1_250_000
    assert mp.meta_cost_microcents(0, 1_000_000) == 4_250_000


def test_is_meta_model():
    assert mp.is_meta_model("meta/muse-spark-1.1")
    assert not mp.is_meta_model("amazon-bedrock/anthropic.claude")


def test_reserve_is_positive_and_bounded():
    # worst case one turn: full context as cache-read + max output
    assert 0 < mp.RESERVE_MICROCENTS <= 2_000_000  # < $2


def test_utc_today_is_iso_date():
    d = mp.utc_today()
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"
