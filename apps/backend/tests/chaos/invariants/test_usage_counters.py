"""Property: usage counters never drift.

INVARIANTS (per owner+period):
  * owner period total == sum of token volume over all RECORDED events
    (record_usage skips events with zero tokens AND zero spend)
  * owner period total == sum of per-member row totals (none lost, none double-counted)
  * request_count == number of recorded events

@given reuses the function-scoped chaos_ddb fixture across examples (and
Hypothesis shrinks any nonce toward collisions), so we reset the owner's rows
at the start of every example via the real delete_all_for_owner — each example
starts from a clean slate for this owner."""

from collections import defaultdict

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.chaos import strategies as ss

_FIELDS = ("input_tokens", "output_tokens", "cache_read", "cache_write")
_OWNER = "org_chaos_usage"


@pytest.mark.chaos
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(
    events=st.lists(st.tuples(ss.user_ids, ss.token_events), min_size=0, max_size=20),
)
@pytest.mark.asyncio
async def test_owner_total_matches_recorded_and_member_sum(chaos_ddb, events):
    from core.repositories import usage_repo
    from core.services import usage_service
    from core.services.usage_service import _current_period

    owner_id = _OWNER
    period = _current_period()
    # Reset this owner's counters so the persistent moto DB doesn't accumulate
    # across hypothesis examples.
    await usage_repo.delete_all_for_owner(owner_id)

    expected = {f: 0 for f in _FIELDS}
    expected_count = 0
    per_member = defaultdict(lambda: {f: 0 for f in _FIELDS})

    for user_id, ev in events:
        await usage_service.record_usage(
            owner_id=owner_id,
            user_id=user_id,
            model=ev["model"],
            input_tokens=ev["input_tokens"],
            output_tokens=ev["output_tokens"],
            cache_read=ev["cache_read"],
            cache_write=ev["cache_write"],
        )
        # record_usage skips an event with zero token volume and zero spend.
        if not any(ev[f] for f in _FIELDS):
            continue
        expected_count += 1
        for f in _FIELDS:
            expected[f] += ev[f]
            per_member[user_id][f] += ev[f]

    summary = await usage_service.get_usage_summary(owner_id)

    # INVARIANT 1: owner period rollup == sum of recorded token volume.
    assert summary["total_input_tokens"] == expected["input_tokens"]
    assert summary["total_output_tokens"] == expected["output_tokens"]
    assert summary["total_cache_read_tokens"] == expected["cache_read"]
    assert summary["total_cache_write_tokens"] == expected["cache_write"]
    assert summary["request_count"] == expected_count

    # INVARIANT 2: owner rollup == sum of per-member rows (no loss, no double-count).
    member_totals = {f: 0 for f in _FIELDS}
    for user_id in per_member:
        row = await usage_repo.get_period_usage(owner_id, f"member:{user_id}:{period}")
        assert row is not None, f"member row missing for {user_id}"
        member_totals["input_tokens"] += int(row["total_input_tokens"])
        member_totals["output_tokens"] += int(row["total_output_tokens"])
        member_totals["cache_read"] += int(row["total_cache_read_tokens"])
        member_totals["cache_write"] += int(row["total_cache_write_tokens"])
    for f in _FIELDS:
        assert member_totals[f] == expected[f], f"member sum mismatch on {f}"
