"""Single source of truth for Metronome money denominations.

DECISION: Metronome's USD credit type is denominated in CENTS. Grounded in
Metronome's documented alert example payload — threshold 20000 paired with
remaining_balance 5000 (i.e. $200 / $50) — and the same credit type backs
every money field we touch: alert thresholds, commit access-schedule
amounts, getNetBalance ``balance``, and webhook ``remaining_balance``.
Sandbox validation re-confirmed this before cutover.

Isol8's internal ledger and API surface speak MICROCENTS. Every boundary
crossing converts through these two helpers so a denomination correction is
a one-file fix instead of a hunt for scattered constants.
"""

from __future__ import annotations

MICROCENTS_PER_CENT = 10_000


def cents_to_microcents(cents: float | int) -> int:
    """Metronome cents -> internal microcents (webhook remaining_balance,
    getNetBalance balance)."""
    return round(float(cents) * MICROCENTS_PER_CENT)
