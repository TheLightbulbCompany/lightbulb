"""Billing webhook dedup idempotency — under duplicate + out-of-order delivery.

Stripe/Metronome (and Clerk) webhooks arrive duplicated and out of order.
record_event_or_skip is the idempotency primitive every billing handler is
gated on. We deliver a random stream of event ids (with duplicates, arbitrary
order) and assert exactly-once, order-independent dedup.

INVARIANTS:
  * the FIRST delivery of any event_id returns RECORDED
  * EVERY subsequent delivery of that event_id returns ALREADY_SEEN
  * exactly one RECORDED per distinct event_id, regardless of interleaving

NOT covered here (documented follow-up in the findings doc): driving the
subscription-lifecycle + entitlement handlers end-to-end (needs respx-stubbed
Stripe + BillingService construction). This validates the dedup gate they all
sit behind.

The dedup table persists across hypothesis examples (function-scoped moto
fixture), so we reset the fixed event-id pool at the start of every example."""

from collections import defaultdict

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_POOL = [f"evt_{i}" for i in range(5)]


@pytest.mark.chaos
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(deliveries=st.lists(st.sampled_from(_POOL), min_size=0, max_size=25))
@pytest.mark.asyncio
async def test_webhook_dedup_exactly_once(chaos_ddb, deliveries):
    from core.services import webhook_dedup
    from core.services.webhook_dedup import WebhookDedupResult

    # Reset the pool so prior examples don't leak "already seen" state.
    for eid in _POOL:
        await webhook_dedup.delete_event(eid)

    seen: set[str] = set()
    recorded_count: dict[str, int] = defaultdict(int)

    for eid in deliveries:
        result = await webhook_dedup.record_event_or_skip(eid, source="stripe")
        if eid not in seen:
            assert result is WebhookDedupResult.RECORDED, f"first delivery of {eid} not RECORDED"
            seen.add(eid)
        else:
            assert result is WebhookDedupResult.ALREADY_SEEN, f"repeat delivery of {eid} not ALREADY_SEEN"
        if result is WebhookDedupResult.RECORDED:
            recorded_count[eid] += 1

    # exactly one RECORDED per distinct delivered event_id
    for eid in set(deliveries):
        assert recorded_count[eid] == 1, f"{eid} recorded {recorded_count[eid]} times (expected 1)"
