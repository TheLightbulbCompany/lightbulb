"""Free-tier $3/day Meta cost cap: reserve-as-gate at pre-send + reconcile
at post-turn.

``reserve_or_block`` atomically reserves a worst-case turn cost
(``RESERVE_MICROCENTS``) against the owner's daily spend counter *before*
the turn is dispatched. Because ``add_meta_daily_spend`` is an atomic
DynamoDB ADD that returns the new total, the reserve-then-check ordering
serializes concurrent turns for the same owner through the counter itself
-- there is no read-then-check gap for two in-flight turns to both slip
under the cap.

``reconcile`` trues the reserve down to the actual cost once the turn
completes and token usage is known. If the actual cost is unknown (0 or
None -- e.g. the container never reported usage for the turn), reconcile
fails closed: it leaves the worst-case reserve in place rather than risk
under-counting spend against the cap.
"""

from core.billing.meta_pricing import CAP_MICROCENTS, RESERVE_MICROCENTS, utc_today
from core.repositories import billing_repo

# session_key -> (date, reserve_microcents). Single-backend-instance scope;
# a reserve lost to a restart is simply never refunded (fail-closed).
_reserves: dict[str, tuple[str, int]] = {}


async def reserve_or_block(owner_id: str, session_key: str) -> bool:
    """Reserve a worst-case turn cost against today's spend. Returns True
    if the owner is already at/over the daily cap (turn should be
    blocked), False if the reserve was placed and the turn may proceed."""
    date = utc_today()
    new_total = await billing_repo.add_meta_daily_spend(owner_id, date, RESERVE_MICROCENTS)
    spent_before = new_total - RESERVE_MICROCENTS
    if spent_before >= CAP_MICROCENTS:
        # already over budget — roll back this reserve and block
        await billing_repo.add_meta_daily_spend(owner_id, date, -RESERVE_MICROCENTS)
        return True
    _reserves[session_key] = (date, RESERVE_MICROCENTS)
    return False


async def reconcile(owner_id: str, session_key: str, actual_microcents: int) -> None:
    """True the reserve down to the actual turn cost. No-op if
    ``session_key`` has no outstanding reserve (not a capped turn, or
    already settled). Fails closed on unknown cost (0/None): keeps the
    worst-case reserve rather than release it against unknown spend."""
    held = _reserves.pop(session_key, None)
    if held is None:
        return  # not a capped turn (or already settled)
    date, reserve = held
    if actual_microcents and actual_microcents > 0:
        await billing_repo.add_meta_daily_spend(owner_id, date, actual_microcents - reserve)
    # else: FAIL-CLOSED — keep the reserve (do nothing)
