"""Metronome-backed ledger (Phase 2). Implements LedgerBackend.

Metronome is the ledger system-of-record (spec §3.2.3). deplete() pushes one
usage event to /v1/ingest keyed on the Bedrock requestId (34-day idempotent),
then optimistically decrements the local fast-gate flag so the synchronous
chat gate sees the spend before Metronome's webhook arrives. get_balance()
reads the LOCAL fast-gate cache (NOT Metronome's async, no-SLA getNetBalance) —
it IS the synchronous chat/provision gate read path, so it must never block on
the balance API. Metronome stays the system-of-record; the fast-gate is its
disposable projection (fed by the low-balance webhook + this deplete decrement).

Callers depend only on the LedgerBackend interface — swapping from
DdbLedgerBackend to this is a one-line LEDGER_BACKEND flip (the Phase-3
cutover), no caller changes. See docs/superpowers/research/
2026-06-07-metronome-ingest-contract.md §6 for the UsageEvent->Metronome
field mapping.
"""

from __future__ import annotations

from typing import Callable

import httpx

from core.billing import metronome_client
from core.billing.ledger_interface import DepleteResult
from core.billing.metronome_client import MetronomeClient
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.billing.usage_event import UsageEvent
from core.config import settings

EVENT_TYPE = "bedrock_invocation"


class MetronomeLedgerBackend:
    """LedgerBackend over Metronome. See module docstring."""

    def __init__(
        self,
        *,
        client_factory: Callable[[httpx.AsyncClient], MetronomeClient] | None = None,
        fast_gate: MetronomeFastGate | None = None,
    ):
        # Default factory builds a client from settings; tests inject a fake.
        self._client_factory = client_factory or metronome_client.for_settings
        self._fast_gate = fast_gate or MetronomeFastGate()

    def _build_properties(self, event: UsageEvent) -> dict[str, str]:
        """All values stringified — Metronome requires string property values."""
        return {
            "model_id": event.model_id,
            "input_tokens": str(event.input_tokens),
            "output_tokens": str(event.output_tokens),
            "cache_read_tokens": str(event.cache_read_tokens),
            "cache_write_tokens": str(event.cache_write_tokens),
            "raw_cost_microcents": str(event.raw_cost_microcents),
            "billed_microcents": str(event.billed_microcents),
            "markup_multiplier": str(event.markup_multiplier),
            "region": event.region,
            "run_status": event.run_status,
            "source": event.source,
        }

    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Push one usage event to Metronome, then optimistically decrement
        the local fast-gate flag. `applied` is best-effort True: Metronome
        dedups on transaction_id server-side (34 days), so the adapter can't
        tell a fresh apply from a dedup — both return 200."""
        metronome_event = {
            "transaction_id": event.request_id,
            "customer_id": event.owner_id,
            "event_type": EVENT_TYPE,
            "timestamp": event.occurred_at,
            "properties": self._build_properties(event),
        }
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            client = self._client_factory(http)
            await client.ingest([metronome_event])

        new_remaining = await self._fast_gate.decrement(event.owner_id, microcents=event.billed_microcents)
        return DepleteResult(balance_microcents=new_remaining, applied=True)

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        """Owner balance in microcents, read from the LOCAL fast-gate cache.

        The only two production callers — connection_pool._check_credit_gate and
        provision_gate.evaluate_provision_gate — are the synchronous chat and
        provision gates. Per spec §3.2/§3.3 they read the fast-gate projection,
        NEVER Metronome's getNetBalance: that balance API has no low-latency SLA
        (research §3) and would put a blocking, outage-prone HTTPS call on the
        per-message hot path of a 0.5-vCPU box — the exact failure this
        remediation is fighting.

        `consistent=True` (passed by connection_pool so a just-landed top-up
        unblocks the next message) is honored by the fast-gate's
        strongly-consistent DDB read, with no Metronome round trip. Metronome
        stays the system-of-record; the fast-gate is its cheap, webhook-/
        optimistic-decrement-fed projection.
        """
        return await self._fast_gate.get_remaining(owner_id)
