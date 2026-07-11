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

``release`` fully refunds a reserve for a turn that never dispatched to
OpenClaw at all (e.g. the ``chat.send`` RPC itself failed) -- a distinct,
non-fail-closed case from ``reconcile``'s unknown-cost path, because a
turn that never reached the container provably cost $0.
"""

from core.billing.meta_pricing import CAP_MICROCENTS, RESERVE_MICROCENTS, utc_today
from core.repositories import billing_repo

# (owner_id, session_key) -> (date, reserve_microcents). Single-backend-instance
# scope; a reserve lost to a restart is simply never refunded (fail-closed).
# Owner-scoping the key means a reconcile call with a mismatched owner_id (or a
# session_key collision across owners) finds no entry and is a clean no-op --
# it can never pop another owner's reserve and apply its delta to the wrong
# owner's counter.
_reserves: dict[tuple[str, str], tuple[str, int]] = {}


async def reserve_or_block(owner_id: str, session_key: str) -> bool:
    """Reserve a worst-case turn cost against today's spend. Returns True
    if the owner is already at/over the daily cap (turn should be
    blocked), False if the reserve was placed and the turn may proceed."""
    # OpenClaw lowercases the event session key (upstream session-key-utils.ts
    # .toLowerCase()), but the reserve side's build_chat_session_key embeds the
    # original mixed-case Clerk user_id -- normalize here so reserve and
    # reconcile land on the same _reserves key.
    session_key = session_key.lower()
    date = utc_today()
    new_total = await billing_repo.add_meta_daily_spend(owner_id, date, RESERVE_MICROCENTS)
    spent_before = new_total - RESERVE_MICROCENTS
    if spent_before >= CAP_MICROCENTS:
        # already over budget — roll back this reserve and block
        await billing_repo.add_meta_daily_spend(owner_id, date, -RESERVE_MICROCENTS)
        return True
    _reserves[(owner_id, session_key)] = (date, RESERVE_MICROCENTS)
    return False


async def release(owner_id: str, session_key: str) -> None:
    """Fully refund an outstanding reserve — the turn provably never reached
    OpenClaw (e.g. ``chat.send`` dispatch failed before the container ever
    saw it), so it incurred exactly $0 of actual cost. No-op if
    ``(owner_id, session_key)`` has no outstanding reserve (not a capped
    turn, already settled/reconciled, or a mismatched owner_id for this
    session_key).

    Unlike ``reconcile``, this is NOT a fail-closed path: reconcile keeps
    the worst-case reserve when the actual cost is merely *unknown* (the
    turn ran, but usage wasn't reported). Here the cost isn't unknown —
    it's known to be zero, because the turn never dispatched at all. Full
    refund is the correct, safe direction; leaving the reserve in place
    would permanently lock free-tier users out on every dispatch failure
    (e.g. a container flap), even though Isol8 spent nothing on the turn.
    """
    # OpenClaw lowercases the event session key (upstream session-key-utils.ts
    # .toLowerCase()); normalize to match the reserve side's key.
    session_key = session_key.lower()
    held = _reserves.pop((owner_id, session_key), None)
    if held is None:
        return  # nothing to release (not a capped turn, or already settled)
    date, reserve = held
    # Roll back against the STORED date, not utc_today() — a release that
    # runs after midnight must still credit the day the reserve was placed
    # on (mirrors reconcile's same stored-date discipline).
    await billing_repo.add_meta_daily_spend(owner_id, date, -reserve)


async def reconcile(owner_id: str, session_key: str, actual_microcents: int) -> None:
    """True the reserve down to the actual turn cost. No-op if
    ``(owner_id, session_key)`` has no outstanding reserve (not a capped
    turn, already settled, or a mismatched owner_id for this session_key).
    Fails closed on unknown cost (0/None): keeps the worst-case reserve
    rather than release it against unknown spend."""
    # OpenClaw lowercases the event session key (upstream session-key-utils.ts
    # .toLowerCase()); normalize to match the reserve side's key.
    session_key = session_key.lower()
    held = _reserves.pop((owner_id, session_key), None)
    if held is None:
        return  # not a capped turn (or already settled)
    date, reserve = held
    if actual_microcents and actual_microcents > 0:
        await billing_repo.add_meta_daily_spend(owner_id, date, actual_microcents - reserve)
    # else: FAIL-CLOSED — keep the reserve (do nothing)
