"""Schema validation for credit purchase amount caps (audit C4 / M4)."""

REQUEST_ID = "b73fe657-5d11-4c95-928d-68c0cf013931"


def test_auto_reload_request_is_gone():
    """Auto-reload died with the DDB ledger — its request schema must not
    creep back in."""
    import routers.billing as billing

    assert not hasattr(billing, "AutoReloadRequest")
