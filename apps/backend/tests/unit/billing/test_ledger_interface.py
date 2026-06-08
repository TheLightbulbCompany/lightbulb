"""Unit tests for the ledger interface: DepleteResult + get_ledger() factory."""

import pytest

from core.billing.ddb_ledger_backend import DdbLedgerBackend
from core.billing.ledger_interface import DepleteResult, get_ledger


def test_deplete_result_fields():
    r = DepleteResult(balance_microcents=500, applied=True)
    assert r.balance_microcents == 500
    assert r.applied is True


def test_get_ledger_returns_ddb_backend_by_default(monkeypatch):
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    ledger = get_ledger()
    assert isinstance(ledger, DdbLedgerBackend)


def test_get_ledger_honours_ddb_setting(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "ddb")
    assert isinstance(get_ledger(), DdbLedgerBackend)


def test_get_ledger_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "metronome")
    with pytest.raises(ValueError, match="metronome"):
        get_ledger()
