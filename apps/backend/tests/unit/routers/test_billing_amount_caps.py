"""Schema validation for credit purchase amount caps (audit C4 / M4)."""

import pytest
from pydantic import ValidationError


def test_top_up_request_rejects_amount_above_one_thousand_dollars():
    from routers.billing import TopUpRequest

    with pytest.raises(ValidationError):
        TopUpRequest(amount_cents=100_001)


def test_top_up_request_accepts_max_amount():
    from routers.billing import TopUpRequest

    req = TopUpRequest(amount_cents=100_000)
    assert req.amount_cents == 100_000


def test_top_up_request_accepts_min_amount():
    from routers.billing import TopUpRequest

    req = TopUpRequest(amount_cents=500)
    assert req.amount_cents == 500


@pytest.mark.parametrize("field", ["threshold_cents", "amount_cents"])
def test_auto_reload_rejects_amount_above_two_hundred_dollars(field):
    from routers.billing import AutoReloadRequest

    payload: dict = {"enabled": True, "threshold_cents": 1000, "amount_cents": 1000}
    payload[field] = 20_001
    with pytest.raises(ValidationError):
        AutoReloadRequest(**payload)


def test_auto_reload_accepts_max_amount():
    from routers.billing import AutoReloadRequest

    req = AutoReloadRequest(enabled=True, threshold_cents=20_000, amount_cents=20_000)
    assert req.amount_cents == 20_000


def test_auto_reload_disabled_skips_amount_validation_when_none():
    """When disabled, threshold/amount can be omitted entirely."""
    from routers.billing import AutoReloadRequest

    req = AutoReloadRequest(enabled=False)
    assert req.enabled is False


def test_auto_reload_rejects_recharge_amount_below_ten_dollars():
    """Metronome requires the recharge-to target to exceed the threshold by at
    least $10, so the schema floors amount_cents at 1000 — a $5-$9.99 amount is
    rejected at the boundary instead of returning a backend 400 FundingError."""
    from routers.billing import AutoReloadRequest

    with pytest.raises(ValidationError):
        AutoReloadRequest(enabled=True, threshold_cents=500, amount_cents=999)


def test_auto_reload_accepts_min_recharge_amount_of_ten_dollars():
    from routers.billing import AutoReloadRequest

    req = AutoReloadRequest(enabled=True, threshold_cents=500, amount_cents=1000)
    assert req.amount_cents == 1000
