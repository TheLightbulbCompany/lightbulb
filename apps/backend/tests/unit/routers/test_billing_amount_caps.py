"""Schema validation for credit purchase amount caps (audit C4 / M4)."""

import pytest
from pydantic import ValidationError


REQUEST_ID = "b73fe657-5d11-4c95-928d-68c0cf013931"


def test_top_up_request_rejects_amount_above_one_thousand_dollars():
    from routers.billing import TopUpRequest

    with pytest.raises(ValidationError):
        TopUpRequest(amount_cents=100_001, request_id=REQUEST_ID)


def test_top_up_request_accepts_max_amount():
    from routers.billing import TopUpRequest

    req = TopUpRequest(amount_cents=100_000, request_id=REQUEST_ID)
    assert req.amount_cents == 100_000


def test_top_up_request_accepts_min_amount():
    from routers.billing import TopUpRequest

    req = TopUpRequest(amount_cents=500, request_id=REQUEST_ID)
    assert req.amount_cents == 500


def test_auto_reload_request_is_gone():
    """Auto-reload died with the DDB ledger — its request schema must not
    creep back in."""
    import routers.billing as billing

    assert not hasattr(billing, "AutoReloadRequest")
