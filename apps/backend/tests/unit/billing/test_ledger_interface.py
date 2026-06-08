"""Unit tests for the ledger interface: DepleteResult + get_ledger() factory.

Phase 3 cutover: Metronome is the SOLE backend. The DDB backend + the "ddb"
factory branch were deleted, so the factory returns MetronomeLedgerBackend by
default and rejects every non-metronome value.
"""

import pytest

from core.billing.ledger_interface import DepleteResult, get_ledger
from core.billing.metronome_ledger_backend import MetronomeLedgerBackend


def test_deplete_result_fields():
    r = DepleteResult(balance_microcents=500, applied=True)
    assert r.balance_microcents == 500
    assert r.applied is True


def test_get_ledger_returns_metronome_by_default(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    monkeypatch.setattr("core.config.settings.LEDGER_BACKEND", "metronome")
    assert isinstance(get_ledger(), MetronomeLedgerBackend)


def test_get_ledger_honours_metronome_setting(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    assert isinstance(get_ledger(), MetronomeLedgerBackend)


def test_get_ledger_rejects_ddb_now(monkeypatch):
    # "ddb" is no longer a valid backend — the DDB ledger was deleted in Phase 3.
    monkeypatch.setenv("LEDGER_BACKEND", "ddb")
    with pytest.raises(ValueError, match="ddb"):
        get_ledger()


def test_get_ledger_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        get_ledger()
