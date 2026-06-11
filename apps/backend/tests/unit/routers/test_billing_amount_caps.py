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


def test_auto_reload_request_is_gone():
    """Auto-reload died with the DDB ledger — its request schema must not
    creep back in."""
    import routers.billing as billing

    assert not hasattr(billing, "AutoReloadRequest")
