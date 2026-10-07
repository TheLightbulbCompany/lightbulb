import pytest

from routers import billing


@pytest.mark.asyncio
async def test_balance_is_exhausted_at_or_below_reserve(monkeypatch):
    """A balance at or below one reservation is unspendable — the gate would
    refuse the next request — so the banner must show."""
    from core.services.metronome_gate import RESERVE_ESTIMATE_MICROCENTS

    for balance, expected in [
        (RESERVE_ESTIMATE_MICROCENTS - 1, True),
        (-5_000_000_000, True),  # settlement overshoot drove the pool negative
        (RESERVE_ESTIMATE_MICROCENTS, True),
        (RESERVE_ESTIMATE_MICROCENTS + 1, False),
    ]:
        assert billing._is_exhausted(balance) is expected


def test_balance_unknown_is_never_exhausted():
    """Metronome down / no customer yet reads as null. Never show a false
    out-of-credits banner on an indeterminate balance."""
    assert billing._is_exhausted(None) is False
