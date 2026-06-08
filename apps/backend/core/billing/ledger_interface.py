"""Thin ledger interface — the swap seam between the meter and the money store.

`deplete(event)` applies one usage event to the owner's balance, exactly once.
`get_balance(owner_id)` reads it. The concrete backend is chosen by the
`LEDGER_BACKEND` setting: "ddb" (the interim hand-rolled ledger) now, and a
Metronome adapter in Phase 3 — callers depend only on this interface, never on
the backend, so the cutover is a one-line config flip with no caller changes.
"""

from __future__ import annotations

import os
from typing import NamedTuple, Protocol, runtime_checkable

from core.billing.usage_event import UsageEvent
from core.config import settings


class DepleteResult(NamedTuple):
    balance_microcents: int
    """Owner balance after this event (may be negative — the meter is post-hoc)."""

    applied: bool
    """True if this event was newly applied; False if it was a deduplicated replay."""


@runtime_checkable
class LedgerBackend(Protocol):
    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Apply one usage event to the owner's balance, exactly once on
        event.request_id. Idempotent: replaying the same request_id is a
        no-op that returns the current balance with applied=False."""
        ...

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        """Owner balance in microcents (0 if no row). consistent=True forces a
        strongly-consistent read for hot-path gating."""
        ...


def get_ledger() -> LedgerBackend:
    """Return the configured ledger backend. The swap seam for Phase 3."""
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "ddb":
        from core.billing.ddb_ledger_backend import DdbLedgerBackend

        return DdbLedgerBackend()
    raise ValueError(f"Unknown LEDGER_BACKEND {backend!r} (expected 'ddb'; 'metronome' lands in Phase 3)")
