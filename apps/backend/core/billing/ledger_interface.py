"""Thin ledger interface — the per-event seam between the meter and the money store.

`deplete(event)` applies one usage event to the owner's balance, exactly once.
`get_balance(owner_id)` reads it. The backend is Metronome (the sole ledger
system-of-record since the Phase-3 cutover) — callers depend only on this
interface, never on the backend.
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
    """Return the configured ledger backend.

    Phase 3 cutover: Metronome is the SOLE ledger system-of-record. The interim
    hand-rolled DDB ledger was deleted. The only valid LEDGER_BACKEND value is
    "metronome".
    """
    backend = os.environ.get("LEDGER_BACKEND") or settings.LEDGER_BACKEND
    if backend == "metronome":
        from core.billing.metronome_ledger_backend import MetronomeLedgerBackend

        return MetronomeLedgerBackend()
    raise ValueError(
        f"Unknown LEDGER_BACKEND {backend!r} (expected 'metronome'; the DDB ledger was deleted in Phase 3)"
    )
